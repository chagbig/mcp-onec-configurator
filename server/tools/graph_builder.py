# -*- coding: utf-8 -*-
r"""Карта переходов «действие пользователя → факт в метаданных» (план v1.14).

Из двух логов собираются тройки «действие → факт»: клик/клавиша пользователя
(станционный регистратор user_watch) и ближайшее последовавшее изменение
конфигурации (журнал моста journal/<pid>.jsonl — тот же формат, что
Toolkit.md_journal).

Вход A, user JSONL (utf-8): события user_watch вида
  {"t": "<ISO-8601>", "type": "mouse"|"key"|"window", ...}
(формат tools/user_watch.py); битые строки и события без распознаваемой
метки времени молча пропускаются (логи диагностические).

Вход B, журнал моста: UTF-16 JSONL с BOM (пишет мост 1.1+, append; чтение —
journal_dashboard.load_entries: BOM le/be, оборванные хвостовые строки
пропускаются): записи {ts, pid, src, kind, kindName, request, obj{id,name,cls},
parentId, container}; ts — epoch-МИЛЛИСЕКУНДЫ (JS Date.getTime()).

build_transitions(user_jsonl, mdjournal_path, window=2.0) -> list[dict]:
для каждого события журнала с kindName из TRACKED_KINDS (AddChild/
DeleteChild/Changed) ищется ближайшее ПРЕДШЕСТВУЮЩЕЕ событие пользователя
(0 <= gap <= window секунд) — тройка:
  {"action": <событие user_watch как есть>,
   "result": <запись журнала как есть>,
   "gap_s":  <float, секунды от действия до факта>}
Список отсортирован по времени факта (result); пары не расходуются — одно
действие может обслужить несколько фактов (два Changed после одного клика).
События других kindName (PropChanged/Save/...) в карту не попадают.
load_states НЕ входит (вне задачи).

Тайм-парсинг (только stdlib, dateutil нет): parse_ts принимает ISO с «Z»
(-> «+00:00», datetime.fromisoformat), наивные ISO — считаются ЛОКАЛЬНЫМ
временем станции (astimezone) и epoch-мс (число или числовая строка —
формат ts журнала моста); всё приводится к aware-datetime в UTC, поэтому
логи с разными форматами меток сопоставимы независимо от часового пояса.

CLI: python tools/graph_builder.py --user user.jsonl --mdjournal journal.jsonl
     [--window 2.0] [--out graph.json]
rc=0 успех (JSON {window, stats, transitions} в stdout или --out),
rc=2 — ошибка входа (файл не найден, window < 0).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# CLI-запуск (в т.ч. python -I): сосед journal_dashboard импортируемым
sys.path.insert(0, str(Path(__file__).resolve().parent))

from journal_dashboard import load_entries  # noqa: E402 — UTF-16 BOM-журнал моста

#: kindName журнала моста, попадающие в карту переходов.
TRACKED_KINDS = frozenset({"AddChild", "DeleteChild", "Changed"})


def parse_ts(value) -> "datetime | None":
    """Метка времени -> aware datetime (UTC); не распознано -> None.

    ISO-строка: «Z»/«z» -> «+00:00», затем datetime.fromisoformat; наивная
    метка — локальное время станции (astimezone без аргумента). Число или
    числовая строка — epoch-миллисекунды (ts журнала моста, JS
    Date.getTime()). Булево отбрасывается (bool — подкласс int).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s.isdigit():
        return parse_ts(int(s))
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # наивная = локальное время станции
    return dt.astimezone(timezone.utc)


def load_user_events(path: str) -> list:
    """События user_watch из JSONL (utf-8, BOM терпим) с распознаваемым «t».

    Битые строки пропускаются; результат отсортирован по времени (стабильно,
    равные метки — в порядке файла).
    """
    events = []
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and parse_ts(e.get("t")) is not None:
            events.append(e)
    events.sort(key=lambda e: parse_ts(e["t"]))
    return events


def _collect(user_jsonl: str, mdjournal_path: str,
             window: float) -> "tuple[list, dict]":
    """(tripy, stats) — общая работа для build_transitions и CLI."""
    if window < 0:
        raise ValueError("window должен быть >= 0, получено %r" % (window,))
    if not Path(mdjournal_path).is_file():
        # №20 (прогон v1.15): без журнала не молчать — честный отказ с
        # инструкцией (запись включается journal_configure, мостов 1.1+)
        raise ValueError(
            "журнал моста не найден: %s — запись не настроена; включите её "
            "инструментом journal_configure (enabled=true) и повторите "
            "после действий в конфигураторе" % mdjournal_path)
    users = load_user_events(user_jsonl)
    user_pairs = [(parse_ts(e["t"]), e) for e in users]  # по возрастанию t
    md_all = load_entries(mdjournal_path)
    facts = []
    for e in md_all:
        if e.get("kindName") not in TRACKED_KINDS:
            continue
        dt = parse_ts(e.get("ts"))
        if dt is not None:
            facts.append((dt, e))
    facts.sort(key=lambda p: p[0])  # карта отсортирована по времени факта
    transitions = []
    for dt, e in facts:
        best = None  # (ut, ev) — ближайшее предшествующее действие в окне
        for ut, ev in user_pairs:
            if ut > dt:
                break  # пары по возрастанию t — дальше только поздние
            if (dt - ut).total_seconds() <= window:
                best = (ut, ev)  # последнее подходящее = ближайшее
        if best is not None:
            transitions.append({
                "action": best[1],
                "result": e,
                "gap_s": round((dt - best[0]).total_seconds(), 6),
            })
    stats = {"user_events": len(users), "md_events": len(md_all),
             "md_tracked": len(facts), "matched": len(transitions)}
    if not md_all:
        # файл есть, записей нет — не «молча 0 переходов», а предупреждение
        stats["warning"] = ("журнал моста пуст (0 записей) — запись не настроена "
                            "или отключена: включите journal_configure "
                            "(enabled=true) и повторите прогон")
    return transitions, stats


def build_transitions(user_jsonl: str, mdjournal_path: str,
                      window: float = 2.0) -> list:
    """Тройки «действие → факт» (формат — см. докстринг модуля).

    user_jsonl — JSONL-лог user_watch (utf-8); mdjournal_path — журнал моста
    journal/<pid>.jsonl (UTF-16 с BOM); window — окно корреляции, сек.
    window < 0 -> ValueError.
    """
    return _collect(user_jsonl, mdjournal_path, window)[0]


def main(argv: list | None = None) -> int:
    """CLI: --user, --mdjournal (обязательны), --window, --out; rc=0/2."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description="Карта переходов «действие пользователя → факт "
                    "в метаданных» из лога user_watch и журнала моста")
    ap.add_argument("--user", required=True,
                    help="JSONL-лог user_watch (utf-8, события с полем t)")
    ap.add_argument("--mdjournal", required=True,
                    help="журнал моста journal/<pid>.jsonl (UTF-16 с BOM)")
    ap.add_argument("--window", type=float, default=2.0,
                    help="окно корреляции в секундах (по умолчанию 2.0)")
    ap.add_argument("--out", default="",
                    help="путь JSON-вывода (по умолчанию stdout)")
    args = ap.parse_args(argv)
    try:
        transitions, stats = _collect(args.user, args.mdjournal, args.window)
    except (ValueError, OSError) as e:
        print("graph_builder: %s" % e, file=sys.stderr)
        return 2
    res = {"window": args.window, "stats": stats, "transitions": transitions}
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
