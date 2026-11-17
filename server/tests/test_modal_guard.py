# -*- coding: utf-8 -*-
"""В1.17: modal-guard — открытая модалка («хранилище всплывает непредсказуемо»,
владелец) — Esc-сброс рабочим каналом, не помогло → честный отказ modal_open
ДО клавиатурных операций дерева (Insert в модалку уходит непредсказуемо)."""
from __future__ import annotations

import os
import sys
import time
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


class TestModalGuard(unittest.TestCase):
    def setUp(self) -> None:
        from mcp_snegopat.config import Config
        from mcp_snegopat.server import Toolkit
        from fake_bridge import FakeBridge
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-modalguard-")
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
        self.esc_calls = []
        # реальный PowerShell-канал в тестах не вызываем (станция может быть
        # занята владельцем) — подставляем счётчик
        self.tk._send_esc_powershell = self._fake_esc

    def _fake_esc(self, pid: int) -> None:
        self.esc_calls.append(pid)
        if self.fake.modal_mode > 0:
            self.fake.modal_mode -= 1  # «Esc снял одну модалку»

    def tearDown(self) -> None:
        self.fake.stop()
        self.tmp.cleanup()

    def test_md_add_child_refuses_when_modal_stuck(self) -> None:
        # модалка не снимается (Esc не помог) — отказ modal_open, клавиатуры нет
        self.fake.modal_mode = 99
        from mcp_snegopat.bridge import BridgeError
        with self.assertRaises(BridgeError) as cm:
            self.tk.md_add_child(session=os.getpid(), container=0,
                                 owner_path="", kind="Справочники",
                                 name="ТестМодалка")
        self.assertEqual(cm.exception.code, "modal_open")
        self.assertIn("Esc", str(cm.exception))
        self.assertEqual(len(self.esc_calls), 2)  # ровно 2 попытки сброса
        self.assertNotIn("md_add_child", [op for op, _ in self.fake.args_seen])
        self.assertNotIn("tree_nav", [op for op, _ in self.fake.args_seen])

    def test_md_add_child_passes_after_esc_reset(self) -> None:
        # модалка снялась первой попыткой — операция идёт дальше (не modal_open)
        self.fake.modal_mode = 1
        self.fake.objects[""] = {"name": "Конфигурация", "id": "root",
                                 "cls": "Конфигурация"}
        from mcp_snegopat.bridge import BridgeError
        try:
            self.tk.md_add_child(session=os.getpid(), container=0,
                                 owner_path="", kind="Справочники",
                                 name="ТестМодалкаОк")
        except BridgeError as e:
            self.assertNotEqual(e.code, "modal_open")
        self.assertGreaterEqual(len(self.esc_calls), 1)
        self.assertEqual(self.fake.modal_mode, 0)

    def test_ensure_no_modal_clean_pass(self) -> None:
        # модалки нет — Esc не дёргается, мгновенный проход
        self.tk._ensure_no_modal(os.getpid())
        self.assertEqual(self.esc_calls, [])

    def test_menu_invoke_refuses_when_modal_stuck(self) -> None:
        # menu_invoke: guard ДО клика заголовка (клик в модалку непредсказуем)
        self.fake.modal_mode = 99
        card = {"Конфигурация": [{"name": "Окно конфигурации", "enabled": True}],
                "headers_px": {"config": 190}}
        (self.root / "menu_map.json").write_text(
            __import__("json").dumps(card, ensure_ascii=False), encoding="utf-8")
        from mcp_snegopat.bridge import BridgeError
        with self.assertRaises(BridgeError) as cm:
            self.tk.menu_invoke(session=os.getpid(), menu="Конфигурация",
                                item="Окно конфигурации")
        self.assertEqual(cm.exception.code, "modal_open")
        self.assertNotIn("mouse_click", [op for op, _ in self.fake.args_seen])


if __name__ == "__main__":
    unittest.main()
