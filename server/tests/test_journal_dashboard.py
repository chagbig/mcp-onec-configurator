# -*- coding: utf-8 -*-
"""Тесты №7 (journal_dashboard): синтетический журнал, без сети и без моста."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import codecs
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


def _entry(ts: str, src: str, kind: int, kind_name: str,
           name: str = "", cls: str = "", container: str = "") -> dict:
    return {"ts": ts, "pid": 1234, "src": src, "kind": kind,
            "kindName": kind_name,
            "obj": {"id": "00000000-0000-0000-0000-000000000001",
                    "name": name, "cls": cls} if name else None,
            "parentId": "", "container": container}


def _write_journal(path: Path, entries: list[dict], tail_garbage: bool = True,
                   bom_le: bool = True) -> None:
    """Пишет UTF-16 JSONL с BOM — так пишет мост (append-JSONL, UTF-16)."""
    lines = [json.dumps(e, ensure_ascii=False) for e in entries]
    if tail_garbage:
        lines.append('{"ts": "2026-11-13T10:05:00", "pid": 1234, "src": "hu')  # обрыв
    text = "\n".join(lines) + "\n"
    bom = codecs.BOM_UTF16_LE if bom_le else codecs.BOM_UTF16_BE
    with io.open(path, "wb") as fh:
        fh.write(bom + text.encode("utf-16-le" if bom_le else "utf-16-be"))


ENTRIES = [
    _entry("2026-11-13T10:02:00", "agent", 0, "AddChild",
           name="ОтчётПродажи", cls="Отчет", container="Справочник.Номенклатура"),
    _entry("2026-11-13T10:01:00", "human", 1, "PropChanged",
           name="", cls="", container="Документ.ЗаказКлиента"),
    _entry("2026-11-13T10:03:00", "human", 4, "Save",
           name="ДокументЗаказ", cls="Документ", container="Документ.ЗаказКлиента"),
    _entry("2026-11-13T10:04:00", "agent", 6, "AfterSave",
           name="Номенклатура", cls="Справочник", container="Справочник.Номенклатура"),
]


class TestLoadEntries(unittest.TestCase):
    def test_load_le_bom(self) -> None:
        from journal_dashboard import load_entries
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "1234.jsonl"
            _write_journal(p, ENTRIES, bom_le=True)
            got = load_entries(str(p))
        self.assertEqual(len(got), 4)  # битая хвостовая строка пропущена
        ts = [e["ts"] for e in got]
        self.assertEqual(ts, sorted(ts))  # сортировка по ts по возрастанию
        self.assertEqual(ts[0], "2026-11-13T10:01:00")
        self.assertEqual(ts[-1], "2026-11-13T10:04:00")

    def test_load_be_bom(self) -> None:
        from journal_dashboard import load_entries
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "5678.jsonl"
            _write_journal(p, ENTRIES[:2], bom_le=False)
            got = load_entries(str(p))
        self.assertEqual(len(got), 2)
        self.assertEqual({e["src"] for e in got}, {"agent", "human"})

    def test_load_skips_empty_and_broken_lines(self) -> None:
        from journal_dashboard import load_entries
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "99.jsonl"
            good = json.dumps(ENTRIES[0], ensure_ascii=False)
            # пустые строки + мусор между записями
            text = "\n\n%s\n\nnot-json{{{\n%s\n\n" % (good, good)
            with io.open(p, "wb") as fh:
                fh.write(codecs.BOM_UTF16_LE + text.encode("utf-16-le"))
            got = load_entries(str(p))
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["kindName"], "AddChild")


class TestBuildDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from journal_dashboard import build_dashboard
        cls.html = build_dashboard(ENTRIES, title="Тест дашборда")

    def test_contains_object_names(self) -> None:
        self.assertIn("ОтчётПродажи", self.html)
        self.assertIn("Номенклатура", self.html)
        self.assertIn("Документ.ЗаказКлиента", self.html)

    def test_contains_counters_and_kind_breakdown(self) -> None:
        self.assertIn("human", self.html)
        self.assertIn("agent", self.html)
        self.assertIn("всего записей", self.html)
        self.assertIn("PropChanged", self.html)  # разбивка по kindName
        self.assertIn("AddChild", self.html)

    def test_contains_filters_markup(self) -> None:
        self.assertIn('id="q"', self.html)  # текстовый поиск
        self.assertIn('id="f-human"', self.html)  # чекбоксы human/agent
        self.assertIn('id="f-agent"', self.html)
        self.assertIn('id="f-kind"', self.html)  # select kindName
        self.assertIn('<select', self.html)
        self.assertIn('type="checkbox"', self.html)

    def test_html_head_and_no_external_deps(self) -> None:
        self.assertIn('charset="utf-8"', self.html)
        self.assertTrue(self.html.lstrip().lower().startswith("<!doctype html>"))
        self.assertNotIn("http://", self.html)
        self.assertNotIn("https://", self.html)
        self.assertNotIn("<link", self.html)  # нет внешних css/шрифтов
        self.assertNotIn("<img", self.html)  # нет картинок по сети
        self.assertNotIn("fetch(", self.html)  # нет сетевых запросов
        self.assertNotIn("XMLHttpRequest", self.html)
        self.assertIn("<script>", self.html)

    def test_data_embedded_json(self) -> None:
        self.assertIn("var DATA = [", self.html)
        # внутри <script> не должно быть «</script» — раньше времени закроет тег
        # (строковые литералы вида '</span>' в JS — легальны)
        script = self.html.split("<script>", 1)[1]
        self.assertNotIn("</script", script.split("</script>", 1)[0])

    def test_empty_journal(self) -> None:
        from journal_dashboard import build_dashboard
        h = build_dashboard([])
        self.assertIn("нет записей", h)
        self.assertIn("charset=\"utf-8\"", h)


class TestMain(unittest.TestCase):
    def test_main_creates_html(self) -> None:
        from journal_dashboard import main
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "777.jsonl"
            _write_journal(p, ENTRIES)
            rc = main(["--journal", str(p)])
            out = Path(d) / "777-dashboard.html"
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())
            with io.open(out, encoding="utf-8-sig") as fh:
                text = fh.read()
            self.assertIn("ОтчётПродажи", text)
            # BOM в начале файла (utf-8-sig) — проверка сырых байтов
            self.assertEqual(out.read_bytes()[:3], codecs.BOM_UTF8)
            # явный --out тоже работает
            out2 = Path(d) / "custom.html"
            rc2 = main(["--journal", str(p), "--out", str(out2),
                        "--title", "Своё имя"])
            self.assertEqual(rc2, 0)
            with io.open(out2, encoding="utf-8-sig") as fh:
                self.assertIn("Своё имя", fh.read())

    def test_main_missing_journal_rc2(self) -> None:
        from journal_dashboard import main
        rc = main(["--journal", os.path.join(tempfile.gettempdir(),
                                             "нет_такого_журнала_12345.jsonl")])
        self.assertEqual(rc, 2)


if __name__ == '__main__':
    unittest.main()
