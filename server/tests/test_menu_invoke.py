# -*- coding: utf-8 -*-
"""№29 (v1.12): menu_invoke — гварды карты (отсутствие карты/пункта/меню)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

sys_path = str(Path(__file__).resolve().parents[1])
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from mcp_snegopat.bridge import BridgeError
from mcp_snegopat.config import Config
from mcp_snegopat.server import Toolkit


class TestMenuInvokeGuards(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-menu-inv-")
        self.root = Path(self.tmp.name)
        self.cfg = Config(root=self.root, journal_path=self.root / "journal.jsonl")
        self.tk = Toolkit(self.cfg)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_map_rejected(self) -> None:
        with self.assertRaises(BridgeError) as cm:
            self.tk.menu_invoke(session=None, menu="Конфигурация",
                                item="Окно конфигурации")
        self.assertIn("карты меню нет", str(cm.exception))

    def test_unknown_menu_rejected(self) -> None:
        (self.root / "menu_map.json").write_text(
            '{"_meta": {}, "headers_px": {}, "Файл": []}', encoding="utf-8")
        with self.assertRaises(BridgeError) as cm:
            self.tk.menu_invoke(session=None, menu="Чудо", item="X")
        self.assertIn("нет меню", str(cm.exception))

    def test_unknown_item_rejected(self) -> None:
        (self.root / "menu_map.json").write_text(
            '{"Файл": [{"name": "Выход", "enabled": true}]}', encoding="utf-8")
        with self.assertRaises(BridgeError) as cm:
            self.tk.menu_invoke(session=None, menu="Файл", item="Несуществующий")
        self.assertIn("нет пункта", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
