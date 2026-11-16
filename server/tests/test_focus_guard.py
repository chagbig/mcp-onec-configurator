# -*- coding: utf-8 -*-
"""№11 (прогон v1.15): guard фокуса — при focusHwnd=0 (конфигуратор в фоне)
md_add_child/md_delete отказывают ДО клавиатурных операций дерева (раньше — висели).
Синтетика на подставном мосте, платформа не нужна."""
from __future__ import annotations

import os
import sys
import time
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


class TestFocusGuard(unittest.TestCase):
    def setUp(self) -> None:
        from mcp_snegopat.config import Config
        from mcp_snegopat.server import Toolkit
        from fake_bridge import FakeBridge
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-focusguard-")
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

    def test_md_add_child_refuses_when_no_focus(self) -> None:
        # focus_fail_times=99: focus_probe всегда 0 — guard обязан отказать
        # до tree_activate/md_add_child (прежде операция висела)
        self.fake.focus_fail_times = 99
        from mcp_snegopat.bridge import BridgeError
        with self.assertRaises(BridgeError) as cm:
            self.tk.md_add_child(session=os.getpid(), container=0,
                                 owner_path="", kind="Справочники",
                                 name="ТестБезФокуса")
        self.assertEqual(cm.exception.code, "no_input_focus")
        self.assertIn("фокус ввода", str(cm.exception))
        # клавиатурные операции дерева не выполнялись
        self.assertNotIn("md_add_child", [op for op, _ in self.fake.args_seen])
        self.assertNotIn("tree_nav", [op for op, _ in self.fake.args_seen])

    def test_md_add_child_passes_with_focus(self) -> None:
        # обычный фокус — операция доходит до моста (существующее поведение)
        self.fake.objects[""] = {"name": "Конфигурация", "id": "root",
                                 "cls": "Конфигурация"}
        from mcp_snegopat.bridge import BridgeError
        # fake-мост не эмулирует создание — ждём его отказ, но НЕ no_input_focus:
        # это доказывает, что фокус-гвард пропустил операцию дальше
        try:
            self.tk.md_add_child(session=os.getpid(), container=0,
                                 owner_path="", kind="Справочники",
                                 name="ТестСФокусом")
        except BridgeError as e:
            self.assertNotEqual(e.code, "no_input_focus")
        else:
            pass  # у fake-моста md_add_child может быть реализован — тоже ок


if __name__ == "__main__":
    unittest.main()
