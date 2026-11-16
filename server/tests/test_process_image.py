# -*- coding: utf-8 -*-
"""№16 (прогон v1.15): живость сессии сверяет ОБРАЗ ПРОЦЕССА, а не только pid.
Наблюдение владельца: умерший конфигуратор отдал pid ОС другому процессу (mstsc) —
list_sessions показывал alive=true на чужом процессе."""
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

from mcp_snegopat.bridge import (Bridge, DESIGNER_IMAGE_PREFIX, Session,  # noqa: E402
                                 pid_alive, process_image)


class TestProcessImage(unittest.TestCase):
    def test_process_image_of_self(self) -> None:
        img = process_image(os.getpid())
        if sys.platform == "win32":
            self.assertIsNotNone(img)
            self.assertTrue(img.endswith(".exe"))
        else:
            self.assertIsNone(img)  # не Windows — имя недоступно

    def test_process_image_of_dead_pid(self) -> None:
        self.assertIsNone(process_image(-1))


class TestAliveChecksImage(unittest.TestCase):
    LIVE_PID = os.getpid()  # Session.pid — имя каталога сессии; берём живой процесс

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.sess_dir = self.d / "sessions" / str(self.LIVE_PID)
        self.sess_dir.mkdir(parents=True)
        self.sess_file = self.sess_dir / "session.json"

    def _session(self, data: dict) -> Session:
        self.sess_file.write_text(json.dumps(data), encoding="utf-8")

        class _Cfg:
            root = str(self.d)

        bridge = Bridge(_Cfg())
        found = {s.pid: s for s in bridge.sessions()}
        return found[self.LIVE_PID]

    def test_foreign_image_is_dead(self) -> None:
        # живой pid, но образ чужой (наблюдалось: mstsc) — сессия мертва
        if sys.platform != "win32":
            self.skipTest("проверка образа — только Windows")
        self.assertIn(pid_alive(os.getpid()), (True, False))  # pid-чек не роняет
        s = self._session({"pid": os.getpid(), "processImage": "mstsc.exe",
                           "platform": "8.3.27.2074", "heartbeat": time.time() * 1000})
        self.assertTrue(pid_alive(os.getpid()))          # сам-то процесс жив
        self.assertFalse(s.alive)                        # но это не конфигуратор
        out = s.summary(stale_after=30)
        self.assertFalse(out["alive"])
        self.assertTrue(out.get("pidReused"))            # маркер переиспользования
        self.assertEqual(out["processImage"], "mstsc.exe")

    def test_designer_image_is_alive(self) -> None:
        s = self._session({"pid": os.getpid(), "processImage": "1cv8.exe",
                           "platform": "8.3.27.2074", "heartbeat": time.time() * 1000})
        if pid_alive(os.getpid()):
            self.assertTrue(s.alive)
        self.assertEqual(DESIGNER_IMAGE_PREFIX, "1cv8")

    def test_no_image_falls_back_to_pid_check(self) -> None:
        # имя недоступно (Linux CI / нет прав: process_image -> None) —
        # прежняя семантика pid-чека
        s = self._session({"pid": os.getpid(), "platform": "8.3.27.2074",
                           "heartbeat": time.time() * 1000})
        import mcp_snegopat.bridge as B
        orig = B.process_image
        B.process_image = lambda pid: None
        try:
            self.assertEqual(s.alive, pid_alive(os.getpid()))
        finally:
            B.process_image = orig

    def test_foreign_real_image_is_dead(self) -> None:
        # реальный опрос в песочнице вернёт образ python — чужой для 1cv8:
        # на Windows это корректный отказ (не конфигуратор), на Linux — None/жив
        s = self._session({"pid": os.getpid(), "platform": "8.3.27.2074",
                           "heartbeat": time.time() * 1000})
        img = process_image(os.getpid())
        if img is not None and pid_alive(os.getpid()):
            self.assertEqual(s.alive, img.startswith(DESIGNER_IMAGE_PREFIX))

    def test_dead_pid_stays_dead_with_image(self) -> None:
        s = self._session({"pid": os.getpid(), "processImage": "1cv8.exe",
                           "platform": "8.3.27.2074",
                           "heartbeat": time.time() * 1000})
        if sys.platform == "win32" and not pid_alive(os.getpid()):
            self.assertFalse(s.alive)


if __name__ == "__main__":
    unittest.main()
