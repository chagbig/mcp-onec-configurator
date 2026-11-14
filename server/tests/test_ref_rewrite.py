# -*- coding: utf-8 -*-
"""Тесты №17: rewrite_refs (все виды литералов), module_targets_from_dump (синтетика),
md_rename cascade на fake-мосте."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


class TestRewriteRefs(unittest.TestCase):
    def test_document_all_literals(self) -> None:
        from ref_rewrite import rewrite_refs
        t = ("Документы.Расходная.Провести();\r\n"
             "ДокументСсылка.Расходная ПустаяСсылка;\r\n"
             "ДокументОбъект = Документы.Расходная.СоздатьДокумент();\r\n"
             "ВЫБРАТЬ * ИЗ Документ.Расходная КАК Док;\r\n"
             "Тип = \"DocumentRef.Расходная\";\r\n"
             "х = Документ.РасходнаяДругая; // не трогать\r\n"
             "у = Документ.РасходнаяСтараяСсылка; // не трогать (граница слова)")
        r = rewrite_refs(t, "Документ", "Расходная", "РасходнаяНовая")
        self.assertIn("Документы.РасходнаяНовая.Провести()", r)
        self.assertIn("ДокументСсылка.РасходнаяНовая", r)
        self.assertIn("Документ.РасходнаяНовая КАК Док", r)
        self.assertIn('"DocumentRef.РасходнаяНовая"', r)
        self.assertIn("Документ.РасходнаяДругая", r)          # чужое не тронуто
        self.assertIn("Документ.РасходнаяСтараяСсылка", r)    # граница слова
        self.assertNotIn("Документы.Расходная.", r)

    def test_catalog_and_processor(self) -> None:
        from ref_rewrite import rewrite_refs
        r = rewrite_refs("Справочники.Номенклатура; СправочникСсылка.Номенклатура",
                         "Справочник", "Номенклатура", "НомНовая")
        self.assertEqual(r, "Справочники.НомНовая; СправочникСсылка.НомНовая")
        r2 = rewrite_refs("Обработки.Разборка.ПолучитьФорму(); ОбработкаОбъект.Разборка",
                          "Обработка", "Разборка", "Разборка2")
        self.assertEqual(r2, "Обработки.Разборка2.ПолучитьФорму(); ОбработкаОбъект.Разборка2")

    def test_common_module_bare_name_guarded(self) -> None:
        from ref_rewrite import rewrite_refs
        t = "х = ДлинноеИмяМодуля.Функция(); у = ДлинноеИмяМодуляДругой.Ф();"
        r = rewrite_refs(t, "ОбщийМодуль", "ДлинноеИмяМодуля", "МодульНовый",
                         allow_bare=True)
        self.assertIn("х = МодульНовый.Функция()", r)
        self.assertIn("у = ДлинноеИмяМодуляДругой.Ф()", r)   # граница слова
        # без allow_bare — не переписывается
        r2 = rewrite_refs(t, "ОбщийМодуль", "ДлинноеИмяМодуля", "МодульНовый")
        self.assertEqual(r2, t)

    def test_unknown_kind(self) -> None:
        from ref_rewrite import rewrite_refs
        with self.assertRaises(ValueError):
            rewrite_refs("x", "Чудо", "А", "Б")


class TestTargetsFromDump(unittest.TestCase):
    def test_synthetic_dump(self) -> None:
        from ref_rewrite import module_targets_from_dump
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            mod = root / "Documents" / "Расходная" / "Modules" / "ObjectModule.bsl"
            mod.parent.mkdir(parents=True)
            mod.write_text("Документы.Расходная.Провести();", encoding="utf-8-sig")
            mgr = root / "Documents" / "Расходная" / "Modules" / "ManagerModule.bsl"
            mgr.write_text("Процедура БезСсылок(); КонецПроцедуры", encoding="utf-8")
            cm = root / "CommonModules" / "МсрОбщийМодуль" / "Modules" / "Module.bsl"
            cm.parent.mkdir(parents=True)
            cm.write_text("Справочники.Расходная.Найти();", encoding="utf-8-sig")
            form = root / "Documents" / "Расходная" / "Forms" / "ФормаДокумента" \
                / "Ext" / "Form" / "Module.bsl"
            form.parent.mkdir(parents=True)
            form.write_text("Документы.Расходная.Найти();", encoding="utf-8-sig")
            targets, forms = module_targets_from_dump(d, "Расходная")
            by_path = {t["path"]: t for t in targets}
            self.assertIn("Документ.Расходная", by_path)
            self.assertEqual(by_path["Документ.Расходная"]["prop"], "МодульОбъекта")
            self.assertIn("ОбщийМодуль.МсрОбщийМодуль", by_path)
            self.assertEqual(by_path["ОбщийМодуль.МсрОбщийМодуль"]["prop"], "Модуль")
            self.assertEqual(len(forms), 1)  # модуль формы не в targets
            # ManagerModule без литералов — не попал
            self.assertEqual(len(targets), 2)


class TestMdRenameCascade(unittest.TestCase):
    def setUp(self) -> None:
        import os
        import time
        import tempfile
        from mcp_snegopat.config import Config
        from mcp_snegopat.server import Toolkit
        from fake_bridge import FakeBridge
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-snegopat-test-")
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

    def tearDown(self) -> None:
        self.fake.stop()
        self.tmp.cleanup()

    def test_cascade_rewrites_module_with_hash_guard(self) -> None:
        self.fake.objects["Справочник.Старое"] = {
            "name": "Старое", "id": "sr1", "cls": "Справочник",
            "props": [{"name": "Имя", "value": "Старое"}], "modules": []}
        self.fake.objects["ОбщийМодуль.МсрСсылки"] = {
            "name": "МсрСсылки", "id": "om1", "cls": "ОбщийМодуль",
            "props": [], "modules": [{"prop": "Модуль", "hash": "x", "lines": 1}]}
        self.fake.objects["Справочник.Новое"] = {
            "name": "Новое", "id": "sr1", "cls": "Справочник",
            "props": [{"name": "Имя", "value": "Новое"}], "modules": []}
        text = "Справочники.Старое.ПолучитьФорму();"
        self.fake.modules[("ОбщийМодуль.МсрСсылки", "Модуль")] = text
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "CommonModules" / "МсрСсылки" / "Modules" / "Module.bsl"
            src.parent.mkdir(parents=True)
            src.write_text(text, encoding="utf-8-sig")
            res = self.tk.md_rename(path="Справочник.Старое", new_name="Новое",
                                    uuid="sr1", cascade=True, dump_dir=d)
        self.assertEqual(res["set"], "Новое")
        self.assertEqual(res["post"]["name"], "Новое")
        rewritten = [r for r in res["rewritten"] if r["status"] == "rewritten"]
        self.assertEqual(len(rewritten), 1)
        self.assertEqual(rewritten[0]["path"], "ОбщийМодуль.МсрСсылки")
        self.assertEqual(self.fake.modules[("ОбщийМодуль.МсрСсылки", "Модуль")],
                         "Справочники.Новое.ПолучитьФорму();")

    def test_no_cascade_notes(self) -> None:
        self.fake.objects["Справочник.Старое"] = {
            "name": "Старое", "id": "sr2", "cls": "Справочник",
            "props": [{"name": "Имя", "value": "Старое"}], "modules": []}
        res = self.tk.md_rename(path="Справочник.Старое", new_name="Новое", uuid="sr2")
        self.assertIn("НЕ переписаны", res["note"])
        self.assertNotIn("rewritten", res)

    def test_cascade_requires_dump(self) -> None:
        self.fake.objects["Справочник.Старое"] = {
            "name": "Старое", "id": "sr3", "cls": "Справочник",
            "props": [{"name": "Имя", "value": "Старое"}], "modules": []}
        from mcp_snegopat.bridge import BridgeError
        with self.assertRaises(BridgeError) as cm:
            self.tk.md_rename(path="Справочник.Старое", new_name="Новое",
                              uuid="sr3", cascade=True)
        self.assertIn("dump_dir", str(cm.exception))

    def test_delete_literal_action_comment(self) -> None:
        """№17 завершение: md_delete literal_action=comment — строки с литералами
        закомментированы в ссылающихся модулях, каждый — с hash-стражем."""
        import tempfile
        self.fake.objects["Документ.Расходная"] = {
            "name": "Расходная", "id": "doc1", "cls": "Документ",
            "props": [{"name": "Имя", "value": "Расходная"}], "modules": []}
        text = "Документы.Расходная.Провести();" + "\r\n" + "х = 1;" + "\r\n"
        self.fake.modules[("ОбщийМодуль.МсрСсылки", "Модуль")] = text
        # md_object до удаления (confirm-план) и после не нужен — план не зовём
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "CommonModules" / "МсрСсылки" / "Modules" / "Module.bsl"
            src.parent.mkdir(parents=True)
            src.write_text(text, encoding="utf-8-sig")
            res = self.tk.md_delete(path="Документ.Расходная", uuid="doc1",
                                    confirm=True, literal_action="comment",
                                    dump_dir=d)
        self.assertEqual(res["literal_action"], "comment")
        self.assertEqual(len(res["literals_commented"]), 1)
        self.assertEqual(res["literals_commented"][0]["lines_commented"], 1)
        new_text = self.fake.modules[("ОбщийМодуль.МсрСсылки", "Модуль")]
        self.assertTrue(new_text.startswith("// Документы.Расходная.Провести();"))
        self.assertIn("х = 1;", new_text)  # соседние строки не тронуты

    def test_delete_comment_requires_dump(self) -> None:
        from mcp_snegopat.bridge import BridgeError
        with self.assertRaises(BridgeError) as cm:
            self.tk.md_delete(path="Документ.Х", uuid="d1",
                              confirm=True, literal_action="comment")
        self.assertIn("dump_dir", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
