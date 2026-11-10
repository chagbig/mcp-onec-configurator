# -*- coding: utf-8 -*-
"""Тесты №21 (query_validate) и №23 (ib_mgmt) — синтетика + живые skipUnless."""
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

PROBE_IB = r"Y:\Snegopat-mcp\bases\eventlog-tmp"  # живая пустая ИБ для COM-тестов


class TestQueryValidate(unittest.TestCase):
    def test_wrap_first1(self) -> None:
        from query_validate import wrap_first1
        w = wrap_first1(" ВЫБРАТЬ 1 КАК Число; ")
        self.assertIn("ВЫБРАТЬ ПЕРВЫЕ 1 * ИЗ (", w)
        self.assertIn(") КАК валидация", w)
        self.assertFalse(w.rstrip().endswith(";"))

    def test_validate_rejects_empty(self) -> None:
        from query_validate import validate, QueryError
        with self.assertRaises(QueryError):
            validate(r"C:\нет", "  ")

    def test_validate_rejects_mode(self) -> None:
        from query_validate import validate, QueryError
        with self.assertRaises(QueryError):
            validate(r"C:\нет", "ВЫБРАТЬ 1", mode="drop")

    @unittest.skipUnless(os.path.isdir(PROBE_IB), "нет живой ИБ eventlog-tmp")
    def test_live_check_ok(self) -> None:
        from query_validate import validate
        res = validate(PROBE_IB, "ВЫБРАТЬ 1 КАК Число", mode="check")
        self.assertTrue(res["ok"], res)

    @unittest.skipUnless(os.path.isdir(PROBE_IB), "нет живой ИБ eventlog-tmp")
    def test_live_bad_syntax(self) -> None:
        from query_validate import validate
        res = validate(PROBE_IB, "ВЫБРАТЬ ГДЕ ОТКУДА ЧТО", mode="check")
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"])

    def test_cli_offline_clear_error(self) -> None:
        r = subprocess.run(
            [sys.executable, "-I", str(ROOT / "server" / "tools" / "query_validate.py"),
             "--base", r"C:\нет\такой\базы", "--query", "ВЫБРАТЬ 1"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)


class TestIbMgmt(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "новая база")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_plan_delete_missing_dir(self) -> None:
        from ib_mgmt import plan_delete
        plan = plan_delete(self.base)
        self.assertTrue(plan["confirm_required"])
        self.assertIn("не существует", plan["warning"])

    def test_plan_delete_refuses_non_ib(self) -> None:
        from ib_mgmt import plan_delete
        os.makedirs(self.base)
        Path(self.base, "мусор.txt").write_text("x")
        plan = plan_delete(self.base)
        self.assertFalse(plan["is_infobase"])
        self.assertIn("1Cv8.1CD", plan["warning"])

    def test_plan_delete_warns_lock_and_own_bases(self) -> None:
        from ib_mgmt import plan_delete
        os.makedirs(self.base)
        Path(self.base, "1Cv8.1CD").write_bytes(b"\x00")
        plan = plan_delete(self.base)
        self.assertTrue(plan["is_infobase"])
        # own_bases гвард
        own = ['File="C:\\ib\\другая";']
        plan2 = plan_delete(self.base, own)
        self.assertNotIn("ЗАПРЕЩЕНО", plan2.get("warning") or "")
        own_self = ['File="' + self.base + '";']
        plan3 = plan_delete(self.base, own_self)
        self.assertIn("ЗАПРЕЩЕНО", plan3.get("warning") or "")

    def test_delete_guard_non_ib_raises(self) -> None:
        from ib_mgmt import delete, IbError
        os.makedirs(self.base)
        with self.assertRaises(IbError):
            delete(self.base)

    def test_cli_create_delete_roundtrip(self) -> None:
        """Живой CREATEINFOBASE + delete c confirm — полный круг на tmp-каталоге."""
        target = os.path.join(self.tmp.name, "ib_roundtrip")
        exe_args = [sys.executable, "-I",
                    str(ROOT / "server" / "tools" / "ib_mgmt.py")]
        r1 = subprocess.run(exe_args + ["create", target],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=180)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertIn('"created": true', r1.stdout)
        r2 = subprocess.run(exe_args + ["delete", target, "--confirm"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=60)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn('"deleted": true', r2.stdout)
        self.assertFalse(os.path.exists(target))

    def test_cli_delete_no_confirm_is_plan(self) -> None:
        os.makedirs(self.base)
        Path(self.base, "1Cv8.1CD").write_bytes(b"\x00")
        r = subprocess.run(
            [sys.executable, "-I", str(ROOT / "server" / "tools" / "ib_mgmt.py"),
             "delete", self.base],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn('"confirm_required": true', r.stdout)
        self.assertTrue(os.path.exists(self.base))


if __name__ == "__main__":
    unittest.main()
