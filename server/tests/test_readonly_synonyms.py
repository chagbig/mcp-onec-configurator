# -*- coding: utf-8 -*-
"""Тесты №20 (read_only пресет) и №25 (многоязычные синонимы)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from fake_bridge import FakeBridge  # noqa: E402
from mcp_snegopat.bridge import BridgeError  # noqa: E402
from mcp_snegopat.config import Config, load_config  # noqa: E402
from mcp_snegopat.server import Toolkit  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
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


class TestReadOnlyPreset(Base):
    """№20: read_only гейтит мостовые write-op, чтение работает."""

    def test_write_ops_forbidden_in_readonly(self) -> None:
        self.cfg.read_only = True
        for op in ("module_set", "prop_set", "md_add_child", "md_delete",
                   "config_save", "update_db_cfg", "cfgstore_commit",
                   "editor_commit", "prop_set_type"):
            with self.assertRaises(BridgeError) as cm:
                self.tk._call(None, op, {})
            self.assertEqual(cm.exception.code, "forbidden", op)

    def test_read_ops_allowed_in_readonly(self) -> None:
        self.cfg.read_only = True
        # md_tree/timer_stats — read-only op: не должно падать на гейте
        self.tk._call(None, "md_tree", {})          # fake вернёт пустое дерево
        self.tk._call(None, "timer_stats", {})

    def test_compositions_covered(self) -> None:
        """md_clone/md_rename идут через гейтнутые op — автоматически запрещены."""
        self.cfg.read_only = True
        with self.assertRaises(BridgeError):
            self.tk.md_clone(path="Справочник.Х", new_name="К")

    def test_config_key_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg_file = Path(d) / "mcp_snegopat.json"
            cfg_file.write_text('{"read_only": true}', encoding="utf-8")
            cfg = load_config(cfg_file)
        self.assertTrue(cfg.read_only)


class TestMultilangSynonyms(Base):
    """№25: synonyms={"ru","en"} — обе локали в одном внутреннем представлении."""

    def test_ru_str_backward_compatible(self) -> None:
        internal = self.tk._synonym_internal("Товары")
        self.assertIn('{1,"ru","Товары"}', internal)
        self.assertNotIn('"en"', internal)

    def test_ru_en_dict(self) -> None:
        internal = self.tk._synonym_internal({"ru": "Товары", "en": "Products"})
        self.assertIn('{1,"ru","Товары"}', internal)
        self.assertIn('{1,"en","Products"}', internal)
        # порядок локалей: ru всегда первым
        self.assertLess(internal.index('"ru"'), internal.index('"en"'))

    def test_en_only(self) -> None:
        internal = self.tk._synonym_internal({"en": "Products"})
        self.assertIn('{1,"en","Products"}', internal)
        self.assertNotIn('"ru"', internal)

    def test_unknown_lang_rejected(self) -> None:
        with self.assertRaises(BridgeError) as cm:
            self.tk._synonym_internal({"de": "Produkte"})
        self.assertIn("ru, en", str(cm.exception))

    def test_bad_chars_rejected(self) -> None:
        with self.assertRaises(BridgeError):
            self.tk._synonym_internal({"ru": 'с "кавычкой"'})

    def test_md_set_synonym_with_synonyms_param(self) -> None:
        self.fake.objects["Справочник.Х"] = {"name": "Х", "id": "x1", "cls": "Справочник"}
        res = self.tk.md_set_synonym(path="Справочник.Х",
                                     synonyms={"ru": "Товары", "en": "Products"},
                                     expect_id="x1")
        call = [a for op, a in self.fake.args_seen if op == "prop_set_internal"][0]
        self.assertEqual(call["prop"], "Синоним")
        self.assertIn('{1,"ru","Товары"}', call["internal"])
        self.assertIn('{1,"en","Products"}', call["internal"])


if __name__ == "__main__":
    unittest.main()
