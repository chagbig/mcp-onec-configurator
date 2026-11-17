# -*- coding: utf-8 -*-
"""graph_builder — карта переходов «действие пользователя → факт» (v1.14).

Синтетика: user JSONL (utf-8, события {"t": iso, "type": ...}) + журнал моста
(UTF-16 с BOM, записи {ts: epoch-ms, kindName, ...}) во временных файлах.
Проверяется временная корреляция: клик в 12:00:01 → AddChild 12:00:02 →
тройка с gap_s=1.0; клик без ближайшего факта — не попадает; окно 2.0 с
включительной границей; выбор БЛИЖАЙШЕГО предшествующего действия; фильтр
kindName; сортировка по времени факта; формат тройки; CLI (--out, rc).

Метки времени в фикстурах собираются через _ms() с тем же преобразованием,
что и в parse_ts (наивные ISO = локальное время станции), поэтому тесты не
зависят от часового пояса машины.

Прогон: python -m unittest discover -s tests  (из server/, только discover).
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import graph_builder  # noqa: E402
from graph_builder import TRACKED_KINDS, build_transitions, parse_ts  # noqa: E402

# --- фикстуры ---------------------------------------------------------------


def _ms(iso: str) -> int:
    """ISO-метка -> epoch-миллисекунды (наивная = локальная, как в parse_ts)."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp() * 1000)


def _user(path: Path, events: list) -> str:
    """Записать user-лог (utf-8 JSONL), вернуть путь строкой."""
    text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _md(path: Path, records: list) -> str:
    """Записать журнал моста (UTF-16 с BOM JSONL — формат моста), вернуть путь."""
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    path.write_bytes(text.encode("utf-16"))
    return str(path)


def _click(iso: str, kind: str = "mouse") -> dict:
    return {"t": iso, "type": kind, "x": 100, "y": 50}


def _fact(kind_name: str, iso: str, name: str = "МойСправочник") -> dict:
    """Запись журнала моста: ts — epoch-ms, как пишет мост (Date.getTime())."""
    return {"ts": _ms(iso), "pid": 4321, "src": "human", "kind": 0,
            "kindName": kind_name, "request": False,
            "obj": {"id": "abc", "name": name, "cls": "Справочник"},
            "container": "0"}


def _result_ts(triple: dict) -> int:
    return triple["result"]["ts"]


# --- parse_ts ---------------------------------------------------------------


class TestParseTs(unittest.TestCase):
    """Тайм-парсинг: ISO с Z, наивные ISO, epoch-мс, мусор -> None."""

    def test_iso_z_aware_utc(self) -> None:
        dt = parse_ts("2026-11-15T09:00:02Z")
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)
        self.assertEqual(dt.utcoffset().total_seconds(), 0.0)

    def test_iso_explicit_offset_normalized_to_utc(self) -> None:
        """Aware-метки приводятся к UTC: тот же момент, что и Z-запись."""
        dt = parse_ts("2026-11-15T12:00:02+03:00")
        self.assertEqual(dt.utcoffset().total_seconds(), 0.0)
        self.assertEqual(dt, parse_ts("2026-11-15T09:00:02Z"))

    def test_iso_naive_becomes_aware(self) -> None:
        """Наивная метка — локальное время станции: aware, и совпадает с _ms."""
        dt = parse_ts("2026-11-15T12:00:02")
        self.assertIsNotNone(dt.tzinfo)
        self.assertEqual(int(dt.timestamp() * 1000), _ms("2026-11-15T12:00:02"))

    def test_epoch_ms_number_matches_iso(self) -> None:
        """Числовой ts журнала моста (epoch-мс) эквивалентен той же ISO-метке."""
        self.assertEqual(parse_ts(_ms("2026-11-15T09:00:02Z")),
                         parse_ts("2026-11-15T09:00:02Z"))
        self.assertEqual(parse_ts(str(_ms("2026-11-15T09:00:02Z"))),
                         parse_ts("2026-11-15T09:00:02Z"))

    def test_garbage_returns_none(self) -> None:
        for bad in ("не дата", "", None, True, {"t": 1}):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_ts(bad))


# --- build_transitions ------------------------------------------------------


class TestBuildTransitions(unittest.TestCase):
    """Синтетика корреляции: тройки, окно, ближайшее действие, сортировка."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    # ядро сценария из ТЗ: клик в 12:00:01 -> AddChild 12:00:02 -> тройка
    def test_click_then_addchild_triple(self) -> None:
        u = _user(self.d / "u.jsonl", [_click("2026-11-15T12:00:01")])
        m = _md(self.d / "j.jsonl", [_fact("AddChild", "2026-11-15T12:00:02")])
        res = build_transitions(u, m, window=2.0)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["action"], _click("2026-11-15T12:00:01"))
        self.assertEqual(res[0]["result"], _fact("AddChild", "2026-11-15T12:00:02"))
        self.assertEqual(res[0]["gap_s"], 1.0)

    def test_z_iso_user_vs_epoch_md_correlate(self) -> None:
        """ISO с Z у пользователя против epoch-мс у моста —frames совпадают."""
        u = _user(self.d / "u.jsonl", [_click("2026-11-15T09:00:01Z")])
        m = _md(self.d / "j.jsonl", [_fact("Changed", "2026-11-15T09:00:02Z")])
        res = build_transitions(u, m, window=2.0)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["gap_s"], 1.0)

    def test_click_without_near_md_excluded(self) -> None:
        """Клик без ближайшего md-факта в окне — не попадает в карту."""
        u = _user(self.d / "u.jsonl", [_click("2026-11-15T12:00:01")])
        m = _md(self.d / "j.jsonl", [_fact("AddChild", "2026-11-15T12:00:11")])
        self.assertEqual(build_transitions(u, m, window=2.0), [])

    def test_window_boundary_inclusive(self) -> None:
        """gap == window — внутри (включительно), gap > window — мимо."""
        u = _user(self.d / "u.jsonl", [_click("2026-11-15T12:00:01")])
        on_edge = _md(self.d / "j1.jsonl", [_fact("AddChild", "2026-11-15T12:00:03")])
        res = build_transitions(u, on_edge, window=2.0)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["gap_s"], 2.0)
        beyond = _md(self.d / "j2.jsonl", [_fact("AddChild", "2026-11-15T12:00:03.500")])
        self.assertEqual(build_transitions(u, beyond, window=2.0), [])

    def test_nearest_preceding_action_wins(self) -> None:
        """Два клика до факта: берётся БЛИЖАЙШЕЕ предшествующее действие."""
        u = _user(self.d / "u.jsonl", [_click("2026-11-15T12:00:01"),
                                       _click("2026-11-15T12:00:01.500")])
        m = _md(self.d / "j.jsonl", [_fact("AddChild", "2026-11-15T12:00:02")])
        res = build_transitions(u, m, window=2.0)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["action"], _click("2026-11-15T12:00:01.500"))
        self.assertEqual(res[0]["gap_s"], 0.5)

    def test_action_after_fact_not_matched(self) -> None:
        """Действие ПОСЛЕ факта — не «предшествующее», пары нет."""
        u = _user(self.d / "u.jsonl", [_click("2026-11-15T12:00:05")])
        m = _md(self.d / "j.jsonl", [_fact("DeleteChild", "2026-11-15T12:00:02")])
        self.assertEqual(build_transitions(u, m, window=2.0), [])

    def test_kindname_filter(self) -> None:
        """Только AddChild/DeleteChild/Changed; PropChanged/Save — мимо карты."""
        u = _user(self.d / "u.jsonl", [_click("2026-11-15T12:00:01")])
        records = [_fact(k, "2026-11-15T12:00:02")
                   for k in ("PropChanged", "Save", "AddChild",
                             "DeleteChild", "Changed")]
        m = _md(self.d / "j.jsonl", records)
        res = build_transitions(u, m, window=2.0)
        self.assertEqual(sorted(t["result"]["kindName"] for t in res),
                         ["AddChild", "Changed", "DeleteChild"])
        self.assertEqual(TRACKED_KINDS,
                         frozenset({"AddChild", "DeleteChild", "Changed"}))

    def test_sorted_by_result_time(self) -> None:
        """Факты в файле не по порядку — карта отсортирована по времени факта."""
        u = _user(self.d / "u.jsonl", [_click("2026-11-15T12:00:04"),
                                       _click("2026-11-15T12:00:09")])
        m = _md(self.d / "j.jsonl", [_fact("AddChild", "2026-11-15T12:00:10"),
                                     _fact("Changed", "2026-11-15T12:00:05")])
        res = build_transitions(u, m, window=2.0)
        self.assertEqual([_result_ts(t) for t in res],
                         [_ms("2026-11-15T12:00:05"), _ms("2026-11-15T12:00:10")])
        self.assertEqual(res[0]["action"], _click("2026-11-15T12:00:04"))
        self.assertEqual(res[1]["action"], _click("2026-11-15T12:00:09"))

    def test_triple_format(self) -> None:
        """Формат тройки: ровно action/result/gap_s; gap_s — float."""
        u = _user(self.d / "u.jsonl", [_click("2026-11-15T12:00:01")])
        m = _md(self.d / "j.jsonl", [_fact("AddChild", "2026-11-15T12:00:02")])
        res = build_transitions(u, m, window=2.0)
        self.assertEqual(set(res[0].keys()), {"action", "result", "gap_s"})
        self.assertIsInstance(res[0]["gap_s"], float)
        # action/result — исходные события как есть, без переименования полей
        self.assertEqual(res[0]["action"]["type"], "mouse")
        self.assertEqual(res[0]["result"]["obj"]["cls"], "Справочник")

    def test_one_action_many_facts(self) -> None:
        """Пары не расходуются: один клик обслужил два Changed-факта."""
        u = _user(self.d / "u.jsonl", [_click("2026-11-15T12:00:01")])
        m = _md(self.d / "j.jsonl", [_fact("Changed", "2026-11-15T12:00:02", "A"),
                                     _fact("Changed", "2026-11-15T12:00:03", "B")])
        res = build_transitions(u, m, window=2.0)
        self.assertEqual(len(res), 2)
        self.assertEqual([t["result"]["obj"]["name"] for t in res], ["A", "B"])
        self.assertEqual([t["action"] for t in res],
                         [_click("2026-11-15T12:00:01")] * 2)

    def test_broken_lines_skipped(self) -> None:
        """Битые строки обоих логов пропускаются, валидные коррелируют."""
        up = self.d / "u.jsonl"
        up.write_text("это не json\n{\"t\": \"битая\"}\n"
                      + json.dumps(_click("2026-11-15T12:00:01")) + "\n",
                      encoding="utf-8")
        mp = self.d / "j.jsonl"
        text = "мусор\n{\"ts\": обрыв\n" + json.dumps(
            _fact("AddChild", "2026-11-15T12:00:02")) + "\n"
        mp.write_bytes(text.encode("utf-16"))
        res = build_transitions(str(up), str(mp), window=2.0)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["gap_s"], 1.0)

    def test_user_events_without_parseable_t_skipped(self) -> None:
        """Событие без распознаваемой метки не участвует в корреляции."""
        u = _user(self.d / "u.jsonl",
                  [{"type": "mouse"}, {"t": "завтра", "type": "key"},
                   _click("2026-11-15T12:00:01")])
        m = _md(self.d / "j.jsonl", [_fact("AddChild", "2026-11-15T12:00:02")])
        res = build_transitions(u, m, window=2.0)
        self.assertEqual(res[0]["action"], _click("2026-11-15T12:00:01"))

    def test_negative_window_raises(self) -> None:
        u = _user(self.d / "u.jsonl", [_click("2026-11-15T12:00:01")])
        m = _md(self.d / "j.jsonl", [_fact("AddChild", "2026-11-15T12:00:02")])
        with self.assertRaises(ValueError):
            build_transitions(u, m, window=-0.1)

    def test_missing_journal_honest_error(self) -> None:
        # №20 (прогон v1.15): без журнала не молчать — отказ с инструкцией
        u = _user(self.d / "u.jsonl", [_click("2026-11-15T12:00:01")])
        missing = str(self.d / "net-zhurnala.jsonl")
        with self.assertRaises(ValueError) as cm:
            build_transitions(u, missing, window=2.0)
        self.assertIn("journal_configure", str(cm.exception))

    def test_empty_journal_warning_in_stats(self) -> None:
        # файл есть, записей 0 — не «молча ноль», а warning в stats
        u = _user(self.d / "u.jsonl", [_click("2026-11-15T12:00:01")])
        m = _md(self.d / "j.jsonl", [])
        import graph_builder as gb
        _, stats = gb._collect(u, m, 2.0)
        self.assertIn("warning", stats)
        self.assertIn("journal_configure", stats["warning"])


# --- CLI --------------------------------------------------------------------


class TestCli(unittest.TestCase):
    """CLI: --user --mdjournal [--window] --out; rc=0 успех, 2 ошибка входа."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.u = _user(self.d / "u.jsonl", [_click("2026-11-15T12:00:01")])
        self.m = _md(self.d / "j.jsonl", [_fact("AddChild", "2026-11-15T12:00:02")])

    def test_cli_writes_out_rc0(self) -> None:
        out = self.d / "graph.json"
        rc = graph_builder.main(["--user", self.u, "--mdjournal", self.m,
                                 "--window", "2", "--out", str(out)])
        self.assertEqual(rc, 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["window"], 2.0)
        self.assertEqual(data["stats"]["matched"], 1)
        self.assertEqual(data["stats"]["user_events"], 1)
        self.assertEqual(data["stats"]["md_tracked"], 1)
        self.assertEqual(len(data["transitions"]), 1)
        self.assertEqual(data["transitions"][0]["gap_s"], 1.0)

    def test_cli_stdout_rc0(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = graph_builder.main(["--user", self.u, "--mdjournal", self.m])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["stats"]["md_events"], 1)
        self.assertEqual(len(data["transitions"]), 1)

    def test_cli_missing_input_rc2(self) -> None:
        rc = graph_builder.main(["--user", str(self.d / "нет.jsonl"),
                                 "--mdjournal", self.m])
        self.assertEqual(rc, 2)

    def test_cli_negative_window_rc2(self) -> None:
        rc = graph_builder.main(["--user", self.u, "--mdjournal", self.m,
                                 "--window", "-1"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
