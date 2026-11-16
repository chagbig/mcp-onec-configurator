# -*- coding: utf-8 -*-
"""№8 (прогон v1.15): run_with_timeout — непрерываемый COM эмулируется сном.
Таймаут даёт честный EventLogError, успех/ошибка пробрасываются как есть."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from eventlog_cfg import run_with_timeout, EventLogError  # noqa: E402


class TestRunWithTimeout(unittest.TestCase):
    def test_success_passthrough(self) -> None:
        self.assertEqual(run_with_timeout(lambda: 42, 10, "тест"), 42)

    def test_error_passthrough(self) -> None:
        def boom():
            raise EventLogError("COMConnect: отказ")
        with self.assertRaises(EventLogError) as cm:
            run_with_timeout(boom, 10, "тест")
        self.assertIn("COMConnect", str(cm.exception))

    def test_timeout_honest_error(self) -> None:
        def stuck():
            time.sleep(3.0)
            return "никогда"
        t0 = time.time()
        with self.assertRaises(EventLogError) as cm:
            run_with_timeout(stuck, 1.0, "Валидация запроса (COM)")
        self.assertIn("не завершилась за 1 с", str(cm.exception))
        self.assertIn("не прерываем", str(cm.exception))
        self.assertLess(time.time() - t0, 2.5)  # вернулись по таймауту, не по сну

    def test_min_timeout_clamp(self) -> None:
        # timeout_s < 1 зажимается до 1 с (защита от мгновенных отказов)
        self.assertEqual(run_with_timeout(lambda: "ок", 0.01, "тест"), "ок")


if __name__ == "__main__":
    unittest.main()
