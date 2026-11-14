# -*- coding: utf-8 -*-
"""№20 (v1.6.0): пресеты инструментов — apply_presets и сборка build_server
с cfg.presets на fake-мосте."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from mcp_snegopat.server import _TOOLS, PRESETS, apply_presets


class TestApplyPresets(unittest.TestCase):
    def test_no_presets_returns_all(self) -> None:
        self.assertEqual(apply_presets(_TOOLS, []), _TOOLS)

    def test_analysis_subset_is_read_only(self) -> None:
        res = apply_presets(_TOOLS, ["analysis"])
        write_tools = {"module_set", "module_patch", "md_add_child", "md_delete",
                       "md_set_property", "config_save", "form_set_xml",
                       "update_db_cfg", "md_rename", "md_clone"}
        for t in write_tools:
            self.assertNotIn(t, res, "write-инструмент %s попал в analysis" % t)
        for t in ("md_tree", "md_search_code", "call_hierarchy", "md_journal"):
            self.assertIn(t, res)
        # порядок исходного списка сохранён
        self.assertEqual(res, [t for t in _TOOLS if t in set(res)])

    def test_meta_includes_write_and_keeps_read(self) -> None:
        res = set(apply_presets(_TOOLS, ["meta"]))
        for t in ("module_set", "md_add_child", "md_rename", "config_save",
                  "md_tree", "call_hierarchy"):
            self.assertIn(t, res)
        self.assertNotIn("form_set_xml", res)

    def test_union_of_presets(self) -> None:
        res = set(apply_presets(_TOOLS, ["analysis", "forms"]))
        self.assertIn("form_get_xml", res)
        self.assertNotIn("module_set", res)

    def test_all_presets_subset_of_tools(self) -> None:
        known = set(_TOOLS)
        for name, tools in PRESETS.items():
            extra = tools - known
            self.assertEqual(extra, set(),
                             "пресет %s содержит незарегистрированные: %s"
                             % (name, extra))

    def test_unknown_preset_rejected(self) -> None:
        with self.assertRaises(ValueError) as cm:
            apply_presets(_TOOLS, ["чудо"])
        self.assertIn("чудо", str(cm.exception))
        self.assertIn("analysis", str(cm.exception))


class TestBuildServerPresets(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from mcp_snegopat.config import Config
        from mcp_snegopat.server import Toolkit
        from fake_bridge import FakeBridge
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-snegopat-preset-")
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

    def _registered(self, cfg) -> set:
        from mcp_snegopat.server import build_server
        mcp = build_server(cfg)
        tm = getattr(mcp, "_tool_manager", None)
        if tm is not None and hasattr(tm, "_tools"):
            return set(tm._tools)
        raise AssertionError("не удалось перечислить инструменты MCPServer")

    def test_build_server_with_preset(self) -> None:
        cfg = self.cfg
        cfg.presets = ["analysis"]
        reg = self._registered(cfg)
        self.assertIn("md_tree", reg)
        self.assertNotIn("module_set", reg)

    def test_build_server_without_presets_all(self) -> None:
        reg = self._registered(self.cfg)
        self.assertEqual(reg, set(_TOOLS))


if __name__ == "__main__":
    unittest.main()
