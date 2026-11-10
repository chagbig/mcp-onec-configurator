# -*- coding: utf-8 -*-
"""Тесты №22 (yaxunit_run): конфиг, парсер jUnit, CLI; живой прогон — skipUnless."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

JUNIT_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="ЮТОбщий.РасчетСуммы" tests="3" failures="1" errors="0" time="1.5">
    <testcase name="СкладываетПоложительные" classname="ЮТОбщий.РасчетСуммы" time="0.4"/>
    <testcase name="СкладываетОтрицательные" classname="ЮТОбщий.РасчетСуммы" time="0.5"/>
    <testcase name="НулевойАргумент" classname="ЮТОбщий.РасчетСуммы" time="0.6">
      <failure message="Ожидали 0, получили 1" type=" AssertionError">1 ≠ 0</failure>
    </testcase>
  </testsuite>
  <testsuite name="ЮТДругой" tests="1" failures="0" errors="1" time="2.0">
    <testcase name="ПадаетИнициализацией" classname="ЮТДругой" time="2.0">
      <error message="Модуль не скомпилировался" type="">стек</error>
    </testcase>
  </testsuite>
</testsuites>"""


class TestYaxunit(unittest.TestCase):
    def test_build_config(self) -> None:
        from yaxunit_run import build_config
        cfg = build_config(r"C:\отчеты\junit.xml")
        self.assertTrue(cfg["closeAfterTests"])
        self.assertFalse(cfg["showReport"])
        self.assertEqual(cfg["reportFormat"], "jUnit")
        self.assertEqual(cfg["reportPath"], r"C:\отчеты\junit.xml")
        self.assertNotIn("filter", cfg)
        cfg2 = build_config(r"C:\r.xml", tests_path=r"C:\тесты")
        self.assertEqual(cfg2["filter"]["testsPath"], r"C:\тесты")

    def test_parse_junit(self) -> None:
        from yaxunit_run import parse_junit
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "junit.xml")
            Path(p).write_text(JUNIT_SAMPLE, encoding="utf-8")
            s = parse_junit(p)
        self.assertEqual(s["tests"], 4)
        self.assertEqual(s["failures"], 1)
        self.assertEqual(s["errors"], 1)
        self.assertEqual(s["time_s"], 3.5)
        self.assertEqual(len(s["failed"]), 2)
        self.assertEqual(s["failed"][0]["name"], "НулевойАргумент")

    def test_run_rejects_missing_base(self) -> None:
        from yaxunit_run import run, YaxunitError
        with self.assertRaises(YaxunitError):
            run(r"C:\нет\иб", r"C:\нет\YAxUnit.epf",
                os.path.join(tempfile.gettempdir(), "jr.xml"))

    def test_cli_offline_clear_error(self) -> None:
        r = subprocess.run(
            [sys.executable, "-I", str(ROOT / "server" / "tools" / "yaxunit_run.py"),
             "--base", r"C:\нет", "--epf", r"C:\нет.epf", "--report", r"C:\нет\x.xml"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
