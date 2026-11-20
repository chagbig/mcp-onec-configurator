# -*- coding: utf-8 -*-
"""№34 (v1.20): right_audit — матрица доступов. connect_fn-инъекция:
фейковый COM-коннект, вердикты ok/denied/error/auth_error, пароли не в отчёте."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from right_audit import audit, RightAuditError, _classify_error  # noqa: E402


class _FakeConn:
    """Фейковый COM-коннект: поведение по логину."""
    DENY = {"Справочник.СекретныеДанные"}
    BAD_OBJ = {"Справочник.НетТакого"}

    def __init__(self, user: str):
        self.user = user

    def NewObject(self, kind):
        assert kind == "Запрос"
        return self

    Текст = property(lambda self: None, lambda self, v: setattr(self, "_text", v))

    def Выполнить(self):
        q = self._text
        for marker in self.BAD_OBJ:
            if marker in q:
                raise RuntimeError("Объект не найден: %s" % marker)

        class Sel:
            def __init__(self, n):
                self._n = n

            def Количество(self):
                return self._n

        class Res:
            def Выбрать(self):
                return Sel(1)

        if self.user == "readonly" and "СекретныеДанные" in q:
            raise RuntimeError("Операция не может быть выполнена из-за "
                               "недостаточности прав у пользователя")
        return Res()


def make_connect(users_passwords: dict):
    def connect(base, user=None, password=None):
        if users_passwords.get(user) != (password or ""):
            raise RuntimeError("Ошибка аутентификации: неверный пароль")
        return _FakeConn(user)
    return connect


class TestRightAudit(unittest.TestCase):
    PAIRS = [
        {"role": "ПолныеПрава", "user": "admin", "password": "p1"},
        {"role": "ТолькоЧтение", "user": "readonly", "password": "p2"},
        {"role": "ПлохаяУчетка", "user": "ghost", "password": "bad"},
    ]
    OBJECTS = ["Справочник.Валюты", "Справочник.СекретныеДанные",
               "Справочник.НетТакого"]

    def _run(self):
        return audit("Z:/тестовая", self.PAIRS, self.OBJECTS,
                     timeout_s=10, connect_fn=make_connect(
                         {"admin": "p1", "readonly": "p2", "ghost": "wrong"}))

    def test_matrix_ok_and_denied(self) -> None:
        res = self._run()
        admin = next(r for r in res["roles"] if r["role"] == "ПолныеПрава")
        self.assertEqual(admin["objects"]["Справочник.Валюты"]["verdict"], "ok")
        self.assertEqual(admin["objects"]["Справочник.Валюты"]["rows"], 1)
        ro = next(r for r in res["roles"] if r["role"] == "ТолькоЧтение")
        self.assertEqual(ro["objects"]["Справочник.СекретныеДанные"]["verdict"],
                         "denied")
        self.assertIn("прав", ro["objects"]["Справочник.СекретныеДанные"]["detail"])

    def test_auth_error_classified(self) -> None:
        res = self._run()
        ghost = next(r for r in res["roles"] if r["role"] == "ПлохаяУчетка")
        for obj in self.OBJECTS:
            self.assertEqual(ghost["objects"][obj]["verdict"], "auth_error")

    def test_error_classified_for_missing_object(self) -> None:
        res = self._run()
        admin = next(r for r in res["roles"] if r["role"] == "ПолныеПрава")
        self.assertEqual(admin["objects"]["Справочник.НетТакого"]["verdict"],
                         "error")

    def test_summary_counts_and_no_passwords_in_report(self) -> None:
        res = self._run()
        self.assertEqual(res["summary"]["roles"], 3)
        self.assertEqual(res["summary"]["probes"], 9)
        self.assertEqual(res["summary"]["ok"], 3)   # admin×3
        self.assertEqual(res["summary"]["denied"], 1)  # readonly×секретные
        self.assertEqual(res["summary"]["auth_error"], 3)  # ghost×3
        raw = repr(res)
        self.assertNotIn("p1", raw)
        self.assertNotIn("password", raw)

    def test_bad_args(self) -> None:
        with self.assertRaises(RightAuditError):
            audit("Z:/x", [], ["Справочник.А"])
        with self.assertRaises(RightAuditError):
            audit("Z:/x", [{"role": "r", "user": "u"}], [])

    def test_classify_error_units(self) -> None:
        self.assertEqual(_classify_error("недостаточности прав"), "denied")
        self.assertEqual(_classify_error("Ошибка аутентификации"), "auth_error")
        self.assertEqual(_classify_error("Объект не найден"), "error")


if __name__ == "__main__":
    unittest.main()
