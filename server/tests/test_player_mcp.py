# -*- coding: utf-8 -*-
"""№30 (v1.17): MCP-обёртка player — classify/coverage/replay поверх
tools/player.py на подставном мосте. Исполнение replay НЕ проверяет
station-ввод (гвард foreground отклонит) — проверяется честный отказ."""
from __future__ import annotations

import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

NOW = "2026-11-16T12:00:00Z"


def _write_log(path: Path, events: list[dict]) -> str:
    lines = []
    for e in events:
        lines.append(json.dumps(e, ensure_ascii=False))
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


class TestPlayerMcp(unittest.TestCase):
    def setUp(self) -> None:
        from mcp_snegopat.config import Config
        from mcp_snegopat.server import Toolkit
        from fake_bridge import FakeBridge
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-player-mcp-")
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
        self.log = _write_log(self.root / "user.jsonl", [
            {"t": NOW, "type": "mouse", "ev": "down", "button": "left",
             "x": 400, "y": 49, "title": "Конфигуратор - Конфигурация"},
            {"t": NOW, "type": "key", "ev": "down", "vk": 0x2D,
             "title": "Конфигурация <п> — дерево метаданных"},
            {"t": NOW, "type": "wheel", "ev": "down", "delta": -120,
             "title": "Конфигуратор - Конфигурация"},
        ])

    def tearDown(self) -> None:
        self.fake.stop()
        self.tmp.cleanup()

    def test_bad_args(self) -> None:
        from mcp_snegopat.bridge import BridgeError
        with self.assertRaises(BridgeError):
            self.tk.player(user_jsonl="")           # нет лога
        with self.assertRaises(BridgeError):
            self.tk.player(user_jsonl=self.log, action="нет")

    def test_classify_classes_and_stats(self) -> None:
        res = self.tk.player(action="classify", user_jsonl=self.log)
        self.assertEqual(res["total"], 3)
        self.assertEqual(sum(res["stats"].values()), 3)  # все события классифицированы
        classes = [a["class"] for a in res["actions"]]
        self.assertIn("uncovered", classes)  # wheel — нет покрытия
        self.assertTrue(any(c in ("covered_op", "menu_invoke")
                            for c in classes))   # клик меню / Insert в дереве

    def test_coverage_summary_and_uncovered(self) -> None:
        res = self.tk.player(action="coverage", user_jsonl=self.log)
        self.assertIn("stats", res)
        self.assertIn("uncovered", res)
        self.assertGreaterEqual(res["total"], 3)

    def test_replay_dry_run_plan_only(self) -> None:
        res = self.tk.player(action="replay", user_jsonl=self.log,
                             dry_run=True)
        self.assertEqual(res["mode"], "dry-run")
        self.assertGreater(res["planned"], 0)
        self.assertEqual(res["executed"], 0)
        # ни один ввод не ушёл: mouse_click мосту не отправлялся
        self.assertNotIn("mouse_click", [op for op, _ in self.fake.args_seen])

    def test_replay_execute_foreground_guard_refuses(self) -> None:
        # исполнение: гвард foreground отклоняет ввод (текущий foreground —
        # не окно конфигуратора) — честный player_failed, ввода ноль
        if sys.platform != "win32":
            self.skipTest("гвард foreground — только Windows")
        from mcp_snegopat.bridge import BridgeError
        from unittest import mock
        import player as player_mod
        # детерминизм: foreground = чужое окно (реальный может быть конфигуратором)
        with mock.patch.object(player_mod, "real_get_foreground",
                               lambda: "Блокнот - без имени"):
            with self.assertRaises(BridgeError) as cm:
                self.tk.player(action="replay", user_jsonl=self.log,
                               dry_run=False, yes=True)
        self.assertEqual(cm.exception.code, "player_failed")
        self.assertIn("foreground", str(cm.exception).lower())
        self.assertNotIn("mouse_click", [op for op, _ in self.fake.args_seen])


if __name__ == "__main__":
    unittest.main()
