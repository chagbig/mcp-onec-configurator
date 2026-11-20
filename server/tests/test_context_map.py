# -*- coding: utf-8 -*-
"""№33 (v1.20): context_map — карта конфигурации для агента. На fake-мосте:
виды с счётчиками, топ объектов per вид, реквизиты/модули, сортировка по
количеству, фильтр kinds, пустые виды пропускаются."""
from __future__ import annotations

import os
import sys
import time
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


class TestContextMap(unittest.TestCase):
    def setUp(self) -> None:
        from mcp_snegopat.config import Config
        from mcp_snegopat.server import Toolkit
        from fake_bridge import FakeBridge
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-ctxmap-")
        self.root = Path(self.tmp.name)
        self.fake = FakeBridge(self.root, pid=os.getpid()).start()
        self.cfg = Config(root=self.root, journal_path=self.root / "journal.jsonl")
        self.tk = Toolkit(self.cfg)
        for _ in range(300):
            if self.tk.bridge.live_sessions():
                break
            time.sleep(0.01)
        else:
            self.fail("подставной мост не поднялся за 3 с")
        self.fake.kind_items = {
            "Справочники": [{"name": "Валюты", "cls": "Справочник", "id": "s1"},
                            {"name": "Номенклатура", "cls": "Справочник", "id": "s2"}],
            "Документы": [{"name": "ПриходнаяНакладная", "cls": "Документ", "id": "d1"}],
            "Перечисления": [],
        }
        self.fake.objects["Справочник.Валюты"] = {
            "name": "Валюты", "id": "s1", "cls": "Справочник",
            "props": [{"name": "Код"}, {"name": "Наим"}, {"name": "Курс"}],
            "tabular_parts": [],
            "modules": [{"name": "МодульОбъекта"}]}
        self.fake.modules[("Справочник.Валюты", "МодульОбъекта")] = "а\nб\nв\n"

    def tearDown(self) -> None:
        self.fake.stop()
        self.tmp.cleanup()

    def test_kinds_sorted_and_empty_skipped(self) -> None:
        res = self.tk.context_map(session=os.getpid())
        kinds = [k["kind"] for k in res["kinds"]]
        self.assertIn("Справочники", kinds)
        self.assertIn("Документы", kinds)
        self.assertNotIn("Перечисления", kinds)  # пустые виды пропущены
        self.assertEqual(kinds[0], "Справочники")  # сортировка по count desc
        self.assertEqual(res["total_objects"], 3)

    def test_objects_with_props_and_modules(self) -> None:
        res = self.tk.context_map(session=os.getpid(), kinds=["Справочники"])
        top = res["kinds"][0]["top"]
        valyuty = next(o for o in top if o["name"] == "Валюты")
        self.assertEqual(valyuty["props"], 3)
        self.assertEqual(valyuty["modules"][0]["lines"], 4)

    def test_kinds_filter_and_per_kind(self) -> None:
        res = self.tk.context_map(session=os.getpid(), kinds=["Документы"],
                                  per_kind=1)
        self.assertEqual([k["kind"] for k in res["kinds"]], ["Документы"])
        self.assertEqual(len(res["kinds"][0]["top"]), 1)

    def test_with_modules_false_skips_lines(self) -> None:
        res = self.tk.context_map(session=os.getpid(), kinds=["Справочники"],
                                  with_modules=False)
        valyuty = res["kinds"][0]["top"][0]
        self.assertEqual(valyuty["modules"], [])


if __name__ == "__main__":
    unittest.main()
