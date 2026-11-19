# -*- coding: utf-8 -*-
r"""Станционный плеер «Сделай как я» (v1.17): классификация лога пользователя
по принципу «лог → снегопат-путь», план воспроизведения и аудит покрытия моста.

Вход — приватный JSONL-лог станционного регистратора tools/user_watch.py:
  mouse:  {"t", "type": "mouse", "ev": "down"|"up"|"wheel", "x", "y",
           "vk": null, "fg_title"}
  key:    {"t", "type": "key", "vk": <int>, "ev": "down"|"up", "fg_title"}
  window: {"t", "type": "window", "ev": "fg_change", "hwnd", "title"}
Заголовок окна в mouse/key лежит в fg_title, в window — в title; читаются
оба (event_title()). Битые строки пропускаются (лог диагностический), но
файл из одних битых строк — ошибка входа (rc=2).

ТРИ КЛАССА ПРИНЦИПА «лог → снегопат-путь»:
  covered_op  — действие покрыто прямой op моста: Insert/Delete (VK 0x2D/
                0x2E) в окне конфигуратора -> md_add_child/md_delete
                (карта vk_ops; расширяется файлом --map);
  menu_invoke — переход через меню: клик (ev=down) в полосе меню окна
                конфигуратора (y <= menu_area_max_y); suggest {"menu":
                "<канон>"} — канон по словарю заголовков menu_titles
                {«Файл»:»file», «Правка»:»edit», «Конфигурация»:
                «configuration», «Отладка»:»debug», «Снегопат»:»snegopat»};
                точное имя пункта в лог НЕ попадает (регистратор пишет
                только VK/координаты) — TODO v1.18: уточнить по UIA-пробе
                (tools/uia_menu_map.py, docs/uia-phase0-recon.md);
  uncovered   — покрытия нет: произвольный клик вне полосы меню, прочие
                клавиши, открытие диалога (type=window), колесо мыши —
                кандидат в IDEAS.
Каждое событие получает класс; ev=up наследует класс парного down (одно
физическое нажатие = одна классификация, classify_events).

КАРТА ПОКРЫТИЯ — DEFAULT_COVERAGE_MAP, расширяется JSON-файлом (--map):
  {"title_marks": ["Конфигура"],       — список, ЗАМЕНЯЕТСЯ
   "menu_area_max_y": 60,               — число, заменяется
   "menu_titles": {"Сервис": "service"},— словарь, ДОПОЛНЯЕТСЯ
   "vk_ops": {"13": "config_save"}}     — словарь, ДОПОЛНЯЕТСЯ (VK
                                        десятичным числом строкой)
Битый --map (не JSON-объект) -> ValueError -> rc=2.

classify: {"session_pid", "total", "stats": {covered_op, menu_invoke,
uncovered}, "actions": [{"t", "type", "title", "class", "suggest", "why"}],
"diag"}; suggest — {} либо {"op": ...} либо {"menu": ...}.

replay — план воспроизведения: шаги только для ev=down (mouse -> action
"click", key -> "keys") и window (exec=false — диалоги не автоматизированы);
up/wheel в план не попадают. Пауза между шагами — фактический зазор лога,
ограничен REPLAY_MAX_GAP_S. БЕЗОПАСНОСТЬ: по умолчанию dry-run (только
план, send-функции не вызываются); исполнение требует --yes И отсутствия
--dry-run; перед исполнением гвард foreground (check_foreground: в
заголовке активного окна нет маркера «Конфигура» -> отказ, ноль ввода).

ИНЪЕКЦИЯ ИСПОЛНИТЕЛЕЙ (дизайн для тестируемости): replay(send_click,
send_keys, clock, sleep, get_foreground) — параметры; дефолты — заглушки
_stub_click/_stub_keys, которые НИКОГДА ввод не синтезируют: в dry-run они
не вызываются вовсе (план и есть журнал шагов), в режиме исполнения их
передача ЗАПРЕЩЕНА (ValueError — честный отказ вместо тихой имитации).
Реальные фабрики модуля:
  build_keys_sender() — keybd_event VK down+up (Windows-only);
      синтетические КЛАВИШИ конфигуратор принимает со станции (прецедент:
      tools/type_text.py №28, SendInput живьём вводит текст в списки 1С);
  build_click_sender(pid) — КАРКАС с честным отказом: клики со станции 1С
      игнорирует — клик должен выполняться из тика моста (mouse_click) и
      уходить письмом в почтовый каталог сессии <root>/sessions/<pid>/req;
      root станционному CLI неизвестен, мост (mcp_snegopat) не
      импортируется принципиально. РЕАЛЬНОЕ подключение кликов — за
      основным агентом в MCP-обёртке: инъекция своего send_click в
      replay()/replay_cli() или подмена build_click_sender.

coverage — аудит покрытия (главный артефакт релиза): сколько действий
каждого класса + полный список uncovered (title, координаты x/y, why);
с --mdjournal каждый uncovered получает md_followed_s — зазор до ближайшего
СЛЕДУЮЩЕГО факта журнала моста (kindName из TRACKED_KINDS, журнал UTF-16
BOM читает journal_dashboard.load_entries). uncovered с фактом — действие,
которое что-то изменило в метаданных мимо карты: приоритетный кандидат
IDEAS (детальная корреляция остаётся за graph_builder).

CLI:
  python tools/player.py classify --user user.jsonl [--session-pid N]
        [--map m.json] [--out report.json]
  python tools/player.py replay --user user.jsonl --session-pid N
        [--dry-run] [--yes] [--start ISO] [--end ISO] [--max N]
        [--map m.json] [--out plan.json]
  python tools/player.py coverage --user user.jsonl [--mdjournal j.jsonl]
        [--window 2.0] [--map m.json] [--out cov.json]
rc=0 успех; rc=2 — ошибка входа: файл не найден, битый JSONL (0 валидных
событий при наличии нераспознанных строк), битая --map/--start/--end,
журнал --mdjournal не найден, отказ гварда foreground/заглушек.
"""
from __future__ import annotations

import argparse
import copy
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

# CLI-запуск (в т.ч. python -I): соседи graph_builder/journal_dashboard
# должны быть импортируемыми
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from graph_builder import TRACKED_KINDS, parse_ts  # noqa: E402 — ISO/epoch-мс
from journal_dashboard import load_entries  # noqa: E402 — UTF-16 BOM-журнал

#: Классы принципа «лог → снегопат-путь» (порядок фиксирован для stats).
CLASSES = ("covered_op", "menu_invoke", "uncovered")

#: VK-коды деревных операций: Insert — создать объект, Delete — удалить.
VK_INSERT = 0x2D
VK_DELETE = 0x2E

#: Подстрока заголовка, означающая «активно окно конфигуратора»:
#: общий префикс «Конфигурация» и «Конфигуратор» (заметьте: «Конфигурац»
#: в «Конфигуратор» НЕ входит — Конфигура+тор).
CONFIGURATOR_MARK = "Конфигура"

#: Потолок паузы между шагами воспроизведения, сек: длинные паузы записи
#: целиком не воспроизводятся (replay не должен растягиваться на минуты).
REPLAY_MAX_GAP_S = 5.0

#: Карта покрытия «признак действия → снегопат-путь» (MVP; расширяется
#: файлом --map — формат см. load_coverage_map).
#: TODO(v1.18): точные каноны меню уточнить по UIA-пробе меню
#: (tools/uia_menu_map.py / docs/uia-phase0-recon.md) — сейчас словарь
#: содержит известные меню конфигуратора и Снегопата.
DEFAULT_COVERAGE_MAP: dict = {
    # подстроки заголовка окна, означающие «окно конфигуратора активно»
    "title_marks": [CONFIGURATOR_MARK],
    # полоса меню: клики с y <= значения считаются переходом через меню
    "menu_area_max_y": 60,
    # словарь заголовков меню: русское имя -> канон (suggest.menu)
    "menu_titles": {
        "Файл": "file",
        "Правка": "edit",
        "Конфигурация": "configuration",
        "Отладка": "debug",
        "Снегопат": "snegopat",
    },
    # прямые op моста: VK-код (десятичный, строкой — ключ JSON) -> op
    "vk_ops": {
        str(VK_INSERT): "md_add_child",
        str(VK_DELETE): "md_delete",
    },
}


def load_coverage_map(path: str) -> dict:
    """Карта покрытия: копия DEFAULT_COVERAGE_MAP, дополненная JSON-файлом.

    Формат файла (все ключи опциональны): title_marks — список (заменяет),
    menu_area_max_y — число (заменяет), menu_titles и vk_ops — словари
    (дополняют); ключи vk_ops — VK-коды десятичным числом строкой.
    Не JSON / не объект -> ValueError (CLI rc=2).
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ValueError("--map: битый JSON (%s): %s" % (path, e)) from e
    if not isinstance(data, dict):
        raise ValueError("--map: ожидался JSON-объект, получено %s"
                         % type(data).__name__)
    merged = copy.deepcopy(DEFAULT_COVERAGE_MAP)
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def event_title(ev: dict) -> str:
    """Заголовок окна события: window пишет title, mouse/key — fg_title."""
    title = ev.get("title")
    if isinstance(title, str) and title:
        return title
    title = ev.get("fg_title")
    return title if isinstance(title, str) else ""


def load_user_log(path: str) -> "tuple[list[dict], dict]":
    """События user_watch из JSONL (utf-8, BOM терпим) + диагностика чтения.

    Возврат: (events, diag); diag = {"total_lines", "broken_lines"} —
    нераспознанные строки (не JSON / без метки «t») считаются битыми и
    пропускаются. События отсортированы по времени (стабильно).
    """
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    events: list[dict] = []
    total = broken = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        total += 1
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            broken += 1
            continue
        if isinstance(e, dict) and parse_ts(e.get("t")) is not None:
            events.append(e)
        else:
            broken += 1
    events.sort(key=lambda e: parse_ts(e["t"]))
    return events, {"total_lines": total, "broken_lines": broken}


def _res(base: dict, cls: str, suggest: dict, why: str) -> dict:
    """Собрать запись классификации {t, type, title, class, suggest, why}."""
    return {"t": base["t"], "type": base["type"], "title": base["title"],
            "class": cls, "suggest": suggest, "why": why}


def _is_int(value) -> bool:
    """Честный int (bool — подкласс int — не считается)."""
    return isinstance(value, int) and not isinstance(value, bool)


def menu_name_for(title: str, cmap: dict) -> str:
    """Канон меню по словарю заголовков: первое вхождение в title; не
    найдено — "" (имя пункта в лог не попадает, см. TODO в докстринге)."""
    for ru, canon in cmap.get("menu_titles", {}).items():
        if ru and ru in title:
            return canon
    return ""


def classify_event(ev: dict, cmap: dict | None = None) -> dict:
    """Классификация ОДНОГО события по карте покрытия (см. докстринг модуля).

    Правила (первое совпадение):
      key ev=down, vk из vk_ops, окно конфигуратора      -> covered_op,
                                                            suggest {"op": ...};
      mouse ev=down, окно конфигуратора, y <= полосы     -> menu_invoke,
                                                            suggest {"menu": ...};
      window / прочие клики / прочие клавиши / up / wheel -> uncovered.
    """
    cmap = cmap if cmap is not None else DEFAULT_COVERAGE_MAP
    etype = ev.get("type")
    base = {"t": ev.get("t"), "type": etype, "title": event_title(ev)}
    title = base["title"]
    in_conf = any(m and m in title for m in cmap.get("title_marks", [])
                  if isinstance(m, str))
    ev_name = ev.get("ev")

    if etype == "key":
        if ev_name != "down":
            return _res(base, "uncovered", {},
                        "key %r — парный к down, отдельно не воспроизводится"
                        % ev_name)
        vk = ev.get("vk")
        op = cmap.get("vk_ops", {}).get(str(vk)) if _is_int(vk) else None
        if op and in_conf:
            return _res(base, "covered_op", {"op": op},
                        "клавиша vk=%s в окне конфигуратора — прямая op "
                        "моста %s" % (vk, op))
        if in_conf:
            return _res(base, "uncovered", {},
                        "клавиша vk=%s в окне конфигуратора вне карты "
                        "покрытия — кандидат в IDEAS" % vk)
        return _res(base, "uncovered", {},
                    "клавиша вне окна конфигуратора (в заголовке нет "
                    "маркера %r)" % CONFIGURATOR_MARK)

    if etype == "mouse":
        if ev_name == "wheel":
            return _res(base, "uncovered", {},
                        "колесо мыши (прокрутка) — вне MVP воспроизведения")
        if ev_name != "down":
            return _res(base, "uncovered", {},
                        "mouse %r — парный к down, отдельно не "
                        "воспроизводится" % ev_name)
        y = ev.get("y")
        if in_conf and _is_int(y) and y <= cmap.get("menu_area_max_y", 60):
            return _res(base, "menu_invoke",
                        {"menu": menu_name_for(title, cmap)},
                        "клик в полосе меню окна конфигуратора (y=%s <= %s) "
                        "— переход через меню"
                        % (y, cmap.get("menu_area_max_y", 60)))
        if in_conf:
            return _res(base, "uncovered", {},
                        "клик в окне конфигуратора вне полосы меню и вне "
                        "карты (y=%r) — кандидат в IDEAS" % y)
        return _res(base, "uncovered", {},
                    "клик вне окна конфигуратора (в заголовке нет маркера "
                    "%r)" % CONFIGURATOR_MARK)

    if etype == "window":
        return _res(base, "uncovered", {},
                    "смена foreground-окна (открытие диалога) — автоповтор "
                    "диалогов не автоматизирован, кандидат в IDEAS")

    return _res(base, "uncovered", {},
                "тип события не распознан (ожидались mouse/key/window)")


def classify_events(events: list, cmap: dict | None = None) -> list[dict]:
    """Классификация списка событий (поэлементно, порядок сохранён).

    ev=up (mouse/key) наследует класс и suggest парного ПРЕДЫДУЩЕГО down
    того же типа (одно физическое нажатие = одна классификация); why
    помечается «парный up». Без парного down в контексте — обычные правила.
    """
    cmap = cmap if cmap is not None else DEFAULT_COVERAGE_MAP
    out: list[dict] = []
    last_down: dict = {}
    for ev in events:
        etype = ev.get("type")
        if ev.get("ev") == "up" and etype in last_down:
            inherited = dict(last_down[etype])
            inherited["why"] = ("парный up к нажатию: %s" % inherited["why"])
            out.append(inherited)
            continue
        res = classify_event(ev, cmap)
        if ev.get("ev") == "down" and etype in ("mouse", "key"):
            last_down[etype] = dict(res)
        out.append(res)
    return out


def class_stats(actions: list) -> dict:
    """Счётчик действий по классам (все три ключа всегда присутствуют)."""
    stats = {cls: 0 for cls in CLASSES}
    for a in actions:
        if a.get("class") in stats:
            stats[a["class"]] += 1
    return stats


def build_plan(events: list, actions: list, start: "datetime | None" = None,
               end: "datetime | None" = None,
               max_steps: int = 0) -> "tuple[list[dict], dict]":
    """План воспроизведения из events+classify_events(events) (длины равны).

    Шаги: mouse ev=down -> action «click», key ev=down -> «keys»
    (exec=true), window -> «none» (exec=false); mouse/key с ev!=down
    (up/wheel) в план не входят вовсе (учёт в filtered["non_actionable"]).
    --start/--end — окно по времени; --max — лимит ИСПОЛНЯЕМЫХ шагов
    (window-шаги не расходуют лимит). Пауза delay_s — зазор от предыдущего
    шага плана, не больше REPLAY_MAX_GAP_S.
    Возврат: (steps, {"out_of_range", "no_ts", "non_actionable",
    "dropped_max"}).
    """
    if len(events) != len(actions):
        raise ValueError("actions должен быть поэлементной классификацией "
                         "events (%d != %d)" % (len(events), len(actions)))
    steps: list[dict] = []
    filtered = {"out_of_range": 0, "no_ts": 0, "non_actionable": 0,
                "dropped_max": 0}
    budget = max_steps if max_steps and max_steps > 0 else None
    prev_ts: "datetime | None" = None
    for ev, act in zip(events, actions):
        ts = parse_ts(ev.get("t"))
        if ts is None:
            filtered["no_ts"] += 1
            continue
        if start is not None and ts < start:
            filtered["out_of_range"] += 1
            continue
        if end is not None and ts > end:
            filtered["out_of_range"] += 1
            continue
        etype = ev.get("type")
        if etype == "mouse" and ev.get("ev") == "down":
            action, executable = "click", True
        elif etype == "key" and ev.get("ev") == "down":
            action, executable = "keys", True
        elif etype in ("mouse", "key"):
            filtered["non_actionable"] += 1
            continue  # up/wheel — в план не входят
        else:
            action, executable = "none", False
        if executable and budget is not None:
            if budget <= 0:
                filtered["dropped_max"] += 1
                continue
            budget -= 1
        delay = 0.0
        if prev_ts is not None:
            gap = (ts - prev_ts).total_seconds()
            delay = round(min(max(gap, 0.0), REPLAY_MAX_GAP_S), 3)
        step = {"i": len(steps), "t": ev.get("t"), "delay_s": delay,
                "type": etype, "class": act["class"], "action": action,
                "title": act["title"], "suggest": act["suggest"],
                "why": act["why"], "exec": executable}
        if action == "click":
            step["x"] = ev.get("x")
            step["y"] = ev.get("y")
        elif action == "keys":
            step["vk"] = ev.get("vk")
        steps.append(step)
        prev_ts = ts
    return steps, filtered


# --- исполнители (инъекция; см. «ИНЪЕКЦИЯ ИСПОЛНИТЕЛЕЙ» в докстринге) -----


def _stub_click(step: dict) -> dict:
    """Заглушка send_click (дефолт): ввода нет — только описание шага.

    В dry-run не вызывается вовсе (план и есть журнал шагов); в режиме
    исполнения передача заглушек запрещена (см. replay)."""
    return {"stub": True, "action": "click",
            "x": step.get("x"), "y": step.get("y")}


def _stub_keys(step: dict) -> dict:
    """Заглушка send_keys (дефолт): ввода нет — только описание шага."""
    return {"stub": True, "action": "keys", "vk": step.get("vk")}


def check_foreground(get_foreground) -> str:
    """Гвард исполнения: в заголовке foreground-окна должен быть маркер
    конфигуратора; иначе RuntimeError (исполнение отменено до ввода)."""
    title = get_foreground() or ""
    if CONFIGURATOR_MARK not in title:
        raise RuntimeError(
            "гвард foreground: активное окно %r — не конфигуратор (нет "
            "маркера %r); исполнение отменено" % (title[:120],
                                                  CONFIGURATOR_MARK))
    return title


def real_get_foreground() -> str:
    """Заголовок foreground-окна станции (Windows-only; ctypes, как
    fg_title в user_watch). Не Windows -> RuntimeError."""
    if sys.platform != "win32":
        raise RuntimeError(
            "проверка foreground доступна только на Windows (платформа %r)"
            % sys.platform)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR,
                                      ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def build_keys_sender():
    """Фабрика исполнителя клавиш: keybd_event VK down+up (Windows-only).

    В отличие от кликов синтетические КЛАВИШИ конфигуратор принимает со
    станции (прецедент tools/type_text.py №28). Вне win32 фабрика поднимает
    RuntimeError (план/dry-run доступны на любой платформе)."""
    if sys.platform != "win32":
        raise RuntimeError(
            "синтетические клавиши доступны только на Windows (платформа %r)"
            % sys.platform)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    keybd = user32.keybd_event
    keybd.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD,
                      ctypes.c_size_t]
    keybd.restype = None
    keyup = 0x0002  # KEYEVENTF_KEYUP

    def send_keys(step: dict) -> dict:
        """VK-пара down+up в текущий фокус (шаг плана «keys»)."""
        vk = int(step["vk"])
        keybd(vk, 0, 0, 0)          # down
        keybd(vk, 0, keyup, 0)      # up
        return {"vk": vk, "sent": True}

    return send_keys


def build_click_sender(pid: int):
    """Каркас исполнителя кликов — ЧЕСТНО, не готов (см. докстринг модуля).

    Синтетический клик со станции конфигуратор 1С игнорирует: клик должен
    выполняться из тика моста (mouse_click), т.е. запрос уходит письмом в
    почтовый каталог сессии <root>/sessions/<pid>/req (ответ — resp). Root
    у станционного player неизвестен, мост не импортируется — поэтому
    фабрика возвращает исполнителя, который отказывается с инструкцией.
    РЕАЛЬНОЕ подключение делает основной агент в MCP-обёртке: инъекция
    своего send_click в replay()/replay_cli() (точка интеграции)."""
    def send_click(step: dict) -> dict:
        raise RuntimeError(
            "клики со станции 1С игнорирует: шаг «click» (x=%s, y=%s) должен "
            "исполняться тиком моста (mouse_click) через почтовый каталог "
            "сессии <root>/sessions/%d/req; подключите реальный send_click "
            "в MCP-обёртке (интеграция основного агента)"
            % (step.get("x"), step.get("y"), int(pid)))

    return send_click


def replay(events: list, session_pid: int = 0, dry_run: bool = True,
           yes: bool = False, start: "datetime | None" = None,
           end: "datetime | None" = None, max_steps: int = 0,
           cmap: dict | None = None, send_click=_stub_click,
           send_keys=_stub_keys, clock=time.monotonic, sleep=time.sleep,
           get_foreground=None) -> dict:
    """План воспроизведения (dry-run по умолчанию) или исполнение.

    Исполнение = yes=True И dry_run=False; при этом:
      - заглушки (_stub_click/_stub_keys) запрещены -> ValueError;
      - гвард foreground (get_foreground или real_get_foreground) до
        первого ввода; мимо маркера конфигуратора -> RuntimeError;
      - шаги исполняются в порядке плана; перед исполняемым шагом —
        sleep(накопленный зазор от предыдущего исполняемого шага, включая
        паузы window-шагов — темп записи воспроизводится, холостых пауз
        перед no-op нет).
    В dry-run send-функции НЕ вызываются ни разу. clock — источник времени
    для elapsed_s, sleep — пауза между шагами (обе — инъекции для тестов).
    """
    cmap = cmap if cmap is not None else DEFAULT_COVERAGE_MAP
    actions = classify_events(events, cmap)
    steps, filtered = build_plan(events, actions, start, end, max_steps)
    executing = bool(yes) and not bool(dry_run)
    report: dict = {
        "mode": "execute" if executing else "dry-run",
        "session_pid": int(session_pid),
        "total_events": len(events),
        "planned": len(steps),
        "executed": 0,
        "skipped": filtered["out_of_range"] + filtered["dropped_max"],
        "filtered": filtered,
        "foreground": None,
        "stats": class_stats(actions),
        "steps": steps,
        "sent": [],
    }
    if not executing:
        return report

    # --- ИСПОЛНЕНИЕ (только --yes и без --dry-run) ---
    if send_click is _stub_click or send_keys is _stub_keys:
        raise ValueError(
            "исполнение с заглушками запрещено: подставьте реальные "
            "исполнители (build_keys_sender()/build_click_sender(pid) или "
            "инъекции send_click/send_keys из MCP-обёртки — интеграция "
            "основного агента)")
    guard = get_foreground if get_foreground is not None else real_get_foreground
    report["foreground"] = check_foreground(guard)
    t0 = clock()
    pending = 0.0  # паузы window-шагов копятся — темп реальных вводов цел
    for step in steps:
        pending += step["delay_s"]
        if not step["exec"]:
            continue
        if pending > 0:
            sleep(pending)
            pending = 0.0
        if step["action"] == "click":
            result = send_click(step)
        else:
            result = send_keys(step)
        report["sent"].append({"i": step["i"], "action": step["action"],
                               "result": result})
        report["executed"] += 1
    report["elapsed_s"] = round(clock() - t0, 3)
    return report


def _md_enrich(uncovered: list, mdjournal_path: str, window: float) -> dict:
    """Обогатить uncovered полем md_followed_s (зазор до ближайшего
    СЛЕДУЮЩЕГО факта журнала моста в окне window; None — факта нет).

    Факты — записи с kindName из TRACKED_KINDS (AddChild/DeleteChild/
    Changed); журнал UTF-16 BOM читает journal_dashboard.load_entries.
    Файл не найден -> ValueError (честный отказ с инструкцией)."""
    if not Path(mdjournal_path).is_file():
        raise ValueError(
            "журнал моста не найден: %s — запись включается "
            "journal_configure (enabled=true), мостов 1.1+" % mdjournal_path)
    entries = load_entries(mdjournal_path)
    facts: list = []
    for e in entries:
        if e.get("kindName") not in TRACKED_KINDS:
            continue
        dt = parse_ts(e.get("ts"))
        if dt is not None:
            facts.append(dt)
    facts.sort()
    confirmed = 0
    idx = 0  # uncovered отсортированы (события лога) — указатель монотонный
    for item in uncovered:
        it = parse_ts(item.get("t"))
        item["md_followed_s"] = None
        if it is None:
            continue
        while idx < len(facts) and facts[idx] < it:
            idx += 1
        if idx < len(facts):
            gap = (facts[idx] - it).total_seconds()
            if gap <= window:
                item["md_followed_s"] = round(gap, 3)
    confirmed = sum(1 for item in uncovered if item["md_followed_s"] is not None)
    return {"window_s": window, "md_events": len(entries),
            "md_tracked": len(facts), "md_confirmed": confirmed}


def coverage_report(user_jsonl: str, mdjournal_path: str = "",
                    cmap: dict | None = None, window: float = 2.0) -> dict:
    """Аудит покрытия (см. докстринг модуля): классы + список uncovered.

    uncovered-записи: {"t", "type", "title", "x", "y", "why"} (координаты
    только у mouse, у прочих None). С mdjournal_path добавляется блок
    "md" (см. _md_enrich). window < 0 -> ValueError.
    """
    if window < 0:
        raise ValueError("window должен быть >= 0, получено %r" % (window,))
    cmap = cmap if cmap is not None else DEFAULT_COVERAGE_MAP
    events, diag = load_user_log(user_jsonl)
    actions = classify_events(events, cmap)
    stats = class_stats(actions)
    stats["broken_lines"] = diag["broken_lines"]
    uncovered: list[dict] = []
    for ev, act in zip(events, actions):
        if act["class"] != "uncovered":
            continue
        uncovered.append({"t": act["t"], "type": act["type"],
                          "title": act["title"], "x": ev.get("x"),
                          "y": ev.get("y"), "why": act["why"]})
    res: dict = {"total": len(events), "stats": stats,
                 "uncovered": uncovered, "diag": diag}
    if mdjournal_path:
        res["md"] = _md_enrich(uncovered, mdjournal_path, window)
    return res


# --- CLI -------------------------------------------------------------------


def _parse_bound(value: str, what: str) -> "datetime | None":
    """--start/--end: ISO-метка -> datetime (UTC); пусто -> None; мусор ->
    ValueError (CLI rc=2)."""
    if not value:
        return None
    dt = parse_ts(value)
    if dt is None:
        raise ValueError("%s: метка времени не распознана: %r" % (what, value))
    return dt


def _load_or_refuse(path: str) -> "tuple[list[dict], dict]":
    """Лог пользователя: файл из одних битых строк — ошибка входа."""
    events, diag = load_user_log(path)
    if not events and diag["broken_lines"]:
        raise ValueError(
            "битый JSONL: %s — валидных событий 0, нераспознанных строк %d"
            % (path, diag["broken_lines"]))
    return events, diag


def classify_cli(args) -> dict:
    """CLI-слой classify: карта (--map), классификация, отчёт."""
    cmap = load_coverage_map(args.map) if getattr(args, "map", "") else None
    events, diag = _load_or_refuse(args.user)
    actions = classify_events(events, cmap)
    return {"session_pid": int(getattr(args, "session_pid", 0) or 0),
            "total": len(events), "stats": class_stats(actions),
            "actions": actions, "diag": diag}


def replay_cli(args, send_click=None, send_keys=None,
               get_foreground=None) -> dict:
    """CLI-слой replay: лог, окно --start/--end, сборка исполнителей.

    send_click/send_keys/get_foreground — ТОЧКИ ИНЪЕКЦИИ для тестов и
    MCP-обёртки (основной агент); без них в режиме исполнения берутся
    фабрики модуля (build_click_sender(pid)/build_keys_sender(),
    real_get_foreground)."""
    cmap = load_coverage_map(args.map) if getattr(args, "map", "") else None
    events, _diag = _load_or_refuse(args.user)
    start = _parse_bound(getattr(args, "start", ""), "--start")
    end = _parse_bound(getattr(args, "end", ""), "--end")
    dry = bool(getattr(args, "dry_run", False)) or not getattr(args, "yes",
                                                               False)
    if not dry:
        if send_click is None:
            send_click = build_click_sender(int(args.session_pid))
        if send_keys is None:
            send_keys = build_keys_sender()
    else:
        send_click = send_click if send_click is not None else _stub_click
        send_keys = send_keys if send_keys is not None else _stub_keys
    return replay(events, session_pid=int(args.session_pid), dry_run=dry,
                  yes=bool(getattr(args, "yes", False)), start=start,
                  end=end, max_steps=int(getattr(args, "max", 0) or 0),
                  cmap=cmap, send_click=send_click, send_keys=send_keys,
                  get_foreground=get_foreground)


def coverage_cli(args) -> dict:
    """CLI-слой coverage: карта (--map), сводка, md-обогащение."""
    cmap = load_coverage_map(args.map) if getattr(args, "map", "") else None
    return coverage_report(args.user,
                           mdjournal_path=getattr(args, "mdjournal", ""),
                           cmap=cmap, window=float(getattr(args, "window",
                                                           2.0)))


def main(argv: list | None = None) -> int:
    """CLI: подкоманды classify/replay/coverage; rc=0 успех, 2 ошибка входа."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        prog="player",
        description="Плеер «Сделай как я»: классификация лога пользователя "
                    "по принципу «лог → снегопат-путь», план воспроизведения "
                    "и аудит покрытия моста")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("classify", help="классификация событий по классам "
                                        "covered_op/menu_invoke/uncovered")
    p.add_argument("--user", required=True,
                   help="JSONL-лог user_watch (utf-8)")
    p.add_argument("--session-pid", type=int, default=0,
                   help="pid конфигуратора (проходит в отчёт)")
    p.add_argument("--map", default="",
                   help="JSON-файл расширения карты покрытия")
    p.add_argument("--out", default="",
                   help="путь JSON-вывода (по умолчанию stdout)")

    p = sub.add_parser("replay", help="план воспроизведения (dry-run по "
                                      "умолчанию) или исполнение (--yes)")
    p.add_argument("--user", required=True,
                   help="JSONL-лог user_watch (utf-8)")
    p.add_argument("--session-pid", type=int, required=True,
                   help="pid конфигуратора (почтовый каталог сессии)")
    p.add_argument("--dry-run", action="store_true",
                   help="только план (действует даже вместе с --yes)")
    p.add_argument("--yes", action="store_true",
                   help="разрешить исполнение синтетического ввода")
    p.add_argument("--start", default="",
                   help="ISO-метка начала окна воспроизведения")
    p.add_argument("--end", default="",
                   help="ISO-метка конца окна воспроизведения")
    p.add_argument("--max", type=int, default=0,
                   help="лимит исполняемых шагов (0 — без лимита)")
    p.add_argument("--map", default="",
                   help="JSON-файл расширения карты покрытия")
    p.add_argument("--out", default="",
                   help="путь JSON-вывода (по умолчанию stdout)")

    p = sub.add_parser("coverage", help="аудит покрытия: классы + список "
                                        "uncovered (пробелы карты)")
    p.add_argument("--user", required=True,
                   help="JSONL-лог user_watch (utf-8)")
    p.add_argument("--mdjournal", default="",
                   help="журнал моста journal/<pid>.jsonl (UTF-16 BOM) — "
                        "обогащение uncovered полем md_followed_s")
    p.add_argument("--window", type=float, default=2.0,
                   help="окно корреляции с журналом, сек (по умолчанию 2.0)")
    p.add_argument("--map", default="",
                   help="JSON-файл расширения карты покрытия")
    p.add_argument("--out", default="",
                   help="путь JSON-вывода (по умолчанию stdout)")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "classify":
            res = classify_cli(args)
        elif args.cmd == "replay":
            res = replay_cli(args)
        else:
            res = coverage_cli(args)
    except (ValueError, OSError, RuntimeError) as e:
        print("player: %s" % e, file=sys.stderr)
        return 2
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
