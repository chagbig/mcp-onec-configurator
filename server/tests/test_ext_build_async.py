# -*- coding: utf-8 -*-
"""№17 (прогон v1.15): ext_build_from_cf — фоновый запуск/статус.
MCP-клиент рвёт долгий блокирующий запрос своим таймаутом; action="start"
отвечает сразу, action="status" различает running/done/failed по lock+report.
Popen подменяется — реальный build_ext не запускается."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


class _FakeProc:
    def __init__(self, pid: int = 999999):
        self.pid = pid
        self.returncode = 0

    def communicate(self, timeout=None):
        return ("", "")


class TestExtBuildAsync(unittest.TestCase):
    def setUp(self) -> None:
        import time
        from mcp_snegopat.config import Config
        from mcp_snegopat.server import Toolkit
        from fake_bridge import FakeBridge
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-extbuild-")
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
        self.work = self.root / "build"
        self.work.mkdir(parents=True, exist_ok=True)
        self.cf1 = self._mk_file("vendor.cf", 2048)
        self.cf2 = self._mk_file("dev.cf", 4096)

    def tearDown(self) -> None:
        self.fake.stop()
        self.tmp.cleanup()

    def _mk_file(self, name: str, size: int) -> str:
        f = self.root / name
        f.write_bytes(b"x" * size)
        return str(f)

    def _lock(self, pid: int) -> None:
        (self.work / ".build-ext.lock").write_text(str(pid), encoding="utf-8")

    def test_status_idle(self) -> None:
        res = self.tk.ext_build_from_cf(work=str(self.work), action="status")
        self.assertEqual(res["state"], "idle")

    def test_status_running_by_live_pid(self) -> None:
        self._lock(os.getpid())  # живой pid тестового процесса
        (self.work / "build.log").write_text("шаг 1\nшаг 2\n", encoding="utf-8")
        res = self.tk.ext_build_from_cf(work=str(self.work), action="status")
        self.assertEqual(res["state"], "running")
        self.assertEqual(res["pid"], os.getpid())
        self.assertIn("шаг 2", res["log_tail"])

    def test_status_done_by_report(self) -> None:
        (self.work / "report.json").write_text("{}", encoding="utf-8")
        res = self.tk.ext_build_from_cf(work=str(self.work), action="status")
        self.assertEqual(res["state"], "done")
        self.assertIn("report", res)

    def test_status_dead_pid_failed_and_lock_released(self) -> None:
        self._lock(-1)  # мёртвый pid, отчёта нет -> failed
        (self.work / "build.log").write_text("упс\n", encoding="utf-8")
        res = self.tk.ext_build_from_cf(work=str(self.work), action="status")
        self.assertEqual(res["state"], "failed")
        self.assertIn("упс", res["log_tail"])
        self.assertFalse((self.work / ".build-ext.lock").exists())  # work снова свободен

    def test_start_returns_immediately_with_lock(self) -> None:
        with mock.patch("subprocess.Popen", lambda *a, **k: _FakeProc(pid=4242)):
            res = self.tk.ext_build_from_cf(cf1=self.cf1, cf2=self.cf2,
                                            name="Тест", prefix="тст",
                                            work=str(self.work), action="start")
        self.assertTrue(res["started"])
        self.assertEqual(res["pid"], 4242)
        self.assertTrue((self.work / ".build-ext.lock").exists())
        self.assertEqual((self.work / ".build-ext.lock").read_text(encoding="utf-8"),
                         "4242")
        self.assertTrue((self.work / "build.log").exists())

    def test_start_busy_when_lock_held(self) -> None:
        self._lock(os.getpid())  # «чужая» живая сборка
        from mcp_snegopat.bridge import BridgeError
        with self.assertRaises(BridgeError) as cm:
            self.tk.ext_build_from_cf(cf1=self.cf1, cf2=self.cf2,
                                      name="Тест", prefix="тст",
                                      work=str(self.work), action="start")
        self.assertEqual(cm.exception.code, "build_ext_busy")
        self.assertIn("status", str(cm.exception))  # подсказка про action=status


if __name__ == "__main__":
    unittest.main()
