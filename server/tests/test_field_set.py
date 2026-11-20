# -*- coding: utf-8 -*-
"""№32 (v1.19): MCP-обёртка field_set — scan/set через CLI uia_value_probe
(subprocess подменяется; живой UIA в тестах не запускается). Гварды:
action, name без «=», text для set, JSON из tmp-файла."""
from __future__ import annotations

import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


def _fake_run(scan: bool, set_result: dict | None = None):
    """Замена subprocess.run: пишет tmp-JSON в cmd --out и возвращает rc=0."""
    def run(cmd, **kwargs):
        out = cmd[cmd.index("--out") + 1]
        payload = {"pid": int(cmd[cmd.index("--pid") + 1]),
                   "window_title": "Конфигуратор - Конфигурация",
                   "controls": [{"name": "", "type": "EditControl",
                                 "value_available": True, "value": ""},
                                {"name": "Подтверждение пароля", "type": "EditControl",
                                 "value_available": True, "value": ""}],
                   "stats": {"total": 2, "edits": 2, "with_value_pattern": 2},
                   "note": ""}
        if set_result is not None:
            payload["set_result"] = set_result
        Path(out).write_text(json.dumps(payload, ensure_ascii=False),
                             encoding="utf-8")

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()
    return run


class TestFieldSet(unittest.TestCase):
    def setUp(self) -> None:
        import time as _t
        from mcp_snegopat.config import Config
        from mcp_snegopat.server import Toolkit
        from fake_bridge import FakeBridge
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-fieldset-")
        self.root = Path(self.tmp.name)
        self.fake = FakeBridge(self.root, pid=os.getpid()).start()
        self.cfg = Config(root=self.root, journal_path=self.root / "journal.jsonl")
        self.tk = Toolkit(self.cfg)
        for _ in range(300):
            if self.tk.bridge.live_sessions():
                break
            _t.sleep(0.01)
        else:
            self.fail("подставной мост не поднялся за 3 с")

    def tearDown(self) -> None:
        self.fake.stop()
        self.tmp.cleanup()

    def test_bad_args(self) -> None:
        from mcp_snegopat.bridge import BridgeError
        with self.assertRaises(BridgeError):
            self.tk.field_set(action="нет")
        with self.assertRaises(BridgeError):
            self.tk.field_set(action="set")            # ни name, ни index
        with self.assertRaises(BridgeError):
            self.tk.field_set(action="set", index=3)   # без text
        with self.assertRaises(BridgeError):
            self.tk.field_set(action="set", name="Имя=Знач")  # «=» в name

    def test_scan_returns_controls(self) -> None:
        with mock.patch("subprocess.run", _fake_run(scan=True)):
            res = self.tk.field_set(action="scan")
        self.assertEqual(res["action"], "scan")
        self.assertEqual(res["stats"]["with_value_pattern"], 2)
        self.assertEqual(len(res["controls"]), 2)

    def test_set_by_name_password_guard_in_cli(self) -> None:
        # гвард пароля реализован в CLI (check_target) — обёртка передаёт и
        # возвращает честный отказ CLI как есть
        captured = {}

        def run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _fake_run(False, {"set": False, "field": "Подтверждение пароля",
                                     "error": "поле похоже на парольное — запись запрещена"})(cmd, **kwargs)

        with mock.patch("subprocess.run", run):
            res = self.tk.field_set(action="set", name="Подтверждение", text="x")
        self.assertEqual(res["set_result"]["set"], False)
        self.assertIn("парольн", res["set_result"]["error"])

    def test_set_by_name_builds_cli_args(self) -> None:
        captured = {}

        def run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _fake_run(False, {"set": True, "field": "Имя базы",
                                     "error": ""})(cmd, **kwargs)

        with mock.patch("subprocess.run", run):
            res = self.tk.field_set(action="set", name="Имя базы", text="Demo")
        self.assertIn("--set", captured["cmd"])
        self.assertIn("Имя базы=Demo", captured["cmd"])
        self.assertEqual(res["set_result"]["set"], True)

    def test_set_by_index_builds_cli_args(self) -> None:
        captured = {}

        def run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _fake_run(False, {"set": True, "field": "#0",
                                     "error": ""})(cmd, **kwargs)

        with mock.patch("subprocess.run", run):
            self.tk.field_set(action="set", index=0, text="probe")
        self.assertIn("--index", captured["cmd"])
        self.assertIn("probe", captured["cmd"])

    def test_cli_failure_raises(self) -> None:
        from mcp_snegopat.bridge import BridgeError

        def run(cmd, **kwargs):
            class R:
                returncode = 2
                stdout = ""
                stderr = "uia_value_probe: у процесса 0 нет видимых окон"

            return R()

        with mock.patch("subprocess.run", run):
            with self.assertRaises(BridgeError) as cm:
                self.tk.field_set(action="scan")
        self.assertEqual(cm.exception.code, "field_set_failed")


if __name__ == "__main__":
    unittest.main()
