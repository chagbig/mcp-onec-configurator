# -*- coding: utf-8 -*-
"""№130 (spec_check): сверка утверждений ТЗ с метаданными на fake-мосте:
exists/kind/similar/impact/queries, дедуп реестра, вердикты, CLI и rc-коды."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import spec_check  # noqa: E402
from mcp_snegopat.config import Config  # noqa: E402
from mcp_snegopat.server import Toolkit  # noqa: E402
from fake_bridge import FakeBridge  # noqa: E402


def _register_demo(fake: FakeBridge) -> None:
    fake.objects["Справочник.Договоры"] = {
        "name": "Договоры", "id": "agr-uuid-1", "cls": "Справочник",
        "props": [], "modules": []}
    fake.objects["Документ.ЗаказКлиента"] = {
        "name": "ЗаказКлиента", "id": "zk-uuid-2", "cls": "Документ",
        "props": [], "modules": []}
    fake.objects["Справочник.ДоговорыКонтрагентов"] = {
        "name": "ДоговорыКонтрагентов", "id": "agr2-uuid-3", "cls": "Справочник",
        "synonym": "Договоры контрагентов", "props": [], "modules": []}


class TestSpecCheckBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-snegopat-test-")
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
        _register_demo(self.fake)

    def tearDown(self) -> None:
        self.fake.stop()
        self.tmp.cleanup()


class TestVerifyClaims(TestSpecCheckBase):
    def test_existing_object_ok(self) -> None:
        claims = [{"text": "Используется документ ЗаказКлиента",
                   "objects": ["Документ.ЗаказКлиента"]}]
        res = spec_check.verify_claims(self.tk, None, claims)
        self.assertEqual(res["objects_checked"], 1)
        self.assertEqual(res["verdicts"][0]["verdict"], "ok")
        o = res["verdicts"][0]["objects"][0]
        self.assertEqual(o["status"], "exists")
        self.assertEqual(o["uuid"], "zk-uuid-2")
        self.assertEqual(o["cls"], "Документ")

    def test_missing_object(self) -> None:
        claims = [{"text": "Есть справочник НеСуществует",
                   "objects": ["Справочник.НеСуществует"]}]
        res = spec_check.verify_claims(self.tk, None, claims)
        v = res["verdicts"][0]
        self.assertEqual(v["verdict"], "missing_objects")
        self.assertEqual(v["missing"], ["Справочник.НеСуществует"])
        o = v["objects"][0]
        self.assertEqual(o["status"], "missing")
        self.assertEqual(o["uuid"], "")

    def test_unknown_kind_no_crash(self) -> None:
        claims = [{"text": "Есть чудо-объект Х",
                   "objects": ["Чудо.Х", "Справочник.Договоры"]}]
        res = spec_check.verify_claims(self.tk, None, claims)
        v = res["verdicts"][0]
        self.assertEqual(v["verdict"], "warnings")
        by_ref = {o["ref"]: o for o in v["objects"]}
        self.assertEqual(by_ref["Чудо.Х"]["status"], "unknown_kind")
        self.assertEqual(by_ref["Справочник.Договоры"]["status"], "exists")
        self.assertTrue(any(w.startswith("unknown_kind: Чудо.Х")
                            for w in v["warnings"]))

    def test_similar_finds_candidates(self) -> None:
        claims = [{"text": "Новый справочник Договоры",
                   "objects": ["Справочник.Договоры"]}]
        res = spec_check.verify_claims(self.tk, None, claims)
        o = res["verdicts"][0]["objects"][0]
        sim_paths = [s["path"] for s in o["similar"]]
        self.assertIn("Справочник.ДоговорыКонтрагентов", sim_paths)
        self.assertNotIn("Справочник.Договоры", sim_paths)  # сам объект исключён
        # похожий объект не из claim'а -> warnings
        self.assertEqual(res["verdicts"][0]["verdict"], "warnings")
        self.assertTrue(any(w.startswith("similar: «Справочник.ДоговорыКонтрагентов»")
                            for w in res["verdicts"][0]["warnings"]))

    def test_similar_by_synonym(self) -> None:
        # объект с синонимом, содержащим фрагмент (fake-мост ищет по синониму)
        self.fake.objects["Справочник.ХранилищеВариантов"] = {
            "name": "ХранилищеВариантов", "id": "hv-4", "cls": "Справочник",
            "synonym": "Договоры шаблонов отчётов", "props": [], "modules": []}
        claims = [{"text": "В ТЗ «договоры шаблонов» — новый механизм?",
                   "objects": ["Справочник.Договоры"]}]
        res = spec_check.verify_claims(self.tk, None, claims)
        sim_paths = [s["path"]
                     for s in res["verdicts"][0]["objects"][0]["similar"]]
        self.assertIn("Справочник.ХранилищеВариантов", sim_paths)

    def test_short_name_skips_similar(self) -> None:
        self.fake.objects["Справочник.Ол"] = {
            "name": "Ол", "id": "ol-1", "cls": "Справочник",
            "props": [], "modules": []}
        claims = [{"text": "короткое имя", "objects": ["Справочник.Ол"]}]
        res = spec_check.verify_claims(self.tk, None, claims)
        o = res["verdicts"][0]["objects"][0]
        self.assertEqual(o["similar"], [])
        self.assertTrue(any("similar" in n for n in o["notes"]))

    def test_impact_via_stubbed_md_search_code(self) -> None:
        seen: dict = {}

        def fake_search(query, cf_path, limit=50, **kw):
            seen["query"] = query
            seen["cf"] = cf_path
            return {"matches": [{"object": "Документ.ЗаказКлиента",
                                 "hits": [{"line": 1, "col": 1,
                                           "fragment": "Справочники.Договоры.Найти();"}]}],
                    "total_hits": 1}

        self.tk.md_search_code = fake_search  # type: ignore[method-assign]
        claims = [{"text": "Договоры ничем не используются",
                   "objects": ["Справочник.Договоры"]}]
        res = spec_check.verify_claims(self.tk, None, claims, cf="fake.cf")
        self.assertEqual(seen["query"], "Справочники.Договоры")
        self.assertEqual(seen["cf"], "fake.cf")
        o = res["verdicts"][0]["objects"][0]
        self.assertEqual(o["impact"], ["Документ.ЗаказКлиента"])
        self.assertEqual(res["verdicts"][0]["verdict"], "warnings")
        self.assertTrue(any("impact: «Документ.ЗаказКлиента»" in w
                            for w in res["verdicts"][0]["warnings"]))

    def test_no_cf_impact_note(self) -> None:
        claims = [{"text": "Договоры используются в заказе",
                   "objects": ["Справочник.Договоры"]}]
        res = spec_check.verify_claims(self.tk, None, claims)
        o = res["verdicts"][0]["objects"][0]
        self.assertEqual(o["impact"], [])
        self.assertTrue(any("cf не задан" in n for n in o["notes"]))
        # похожий ДоговорыКонтрагентов всё равно даёт warnings
        self.assertEqual(res["verdicts"][0]["verdict"], "warnings")

    def test_impact_objects_in_claim_not_warning(self) -> None:
        self.tk.md_search_code = (  # type: ignore[method-assign]
            lambda query, cf_path, limit=50, **kw:
            {"matches": [{"object": "Документ.ЗаказКлиента",
                          "hits": [{"line": 1, "col": 1, "fragment": "x"}]}],
             "total_hits": 1})
        claims = [{"text": "Договоры используются в ЗаказКлиента",
                   "objects": ["Справочник.Договоры", "Документ.ЗаказКлиента"]}]
        res = spec_check.verify_claims(self.tk, None, claims, cf="fake.cf")
        impact_warns = [w for w in res["verdicts"][0]["warnings"]
                        if w.startswith("impact:")]
        self.assertEqual(impact_warns, [])  # задетый объект — из claim'а

    def test_queries_without_base_noted(self) -> None:
        claims = [{"text": "Запрос из ТЗ", "objects": ["Справочник.Договоры"],
                   "queries": ["ВЫБРАТЬ * ИЗ Справочник.Договоры"]}]
        res = spec_check.verify_claims(self.tk, None, claims)
        self.assertTrue(any("base не задан" in n
                            for n in res["verdicts"][0]["notes"]))

    def test_queries_with_stubbed_validator(self) -> None:
        self.tk.query_validate = (  # type: ignore[method-assign]
            lambda query, base, mode="check", limit=5:
            {"ok": False, "error": "Поле не найдено «Договоры.Сумма»"})
        claims = [{"text": "Запрос из ТЗ",
                   "queries": ["ВЫБРАТЬ Сумма ИЗ Справочник.Договоры"]}]
        res = spec_check.verify_claims(self.tk, None, claims, base="fake.ib")
        v = res["verdicts"][0]
        self.assertFalse(v["queries"][0]["ok"])
        self.assertIn("Поле не найдено", v["queries"][0]["error"])
        self.assertEqual(v["verdict"], "warnings")
        # ok-запрос не даёт warning
        self.tk.query_validate = lambda query, base, mode="check", limit=5: {"ok": True}  # type: ignore
        res2 = spec_check.verify_claims(self.tk, None, claims, base="fake.ib")
        self.assertEqual(res2["verdicts"][0]["verdict"], "ok")

    def test_registry_dedup_across_claims(self) -> None:
        claims = [
            {"text": "Заявление №1", "objects": ["Справочник.Договоры",
                                                 "Документ.ЗаказКлиента"]},
            {"text": "Заявление №2", "objects": ["Документ.ЗаказКлиента",
                                                 "Справочник.НеТакой"]},
        ]
        res = spec_check.verify_claims(self.tk, None, claims)
        self.assertEqual(res["objects_checked"], 3)  # уникальных
        md_object_calls = [a for op, a in self.fake.args_seen
                           if op == "md_object" and a.get("path") == "Справочник.Договоры"]
        self.assertEqual(len(md_object_calls), 1)  # проверен один раз
        self.assertEqual(res["verdicts"][1]["verdict"], "missing_objects")

    def test_claim_without_objects(self) -> None:
        claims = [{"text": "Чисто текстовое утверждение"}]
        res = spec_check.verify_claims(self.tk, None, claims)
        self.assertEqual(res["verdicts"][0]["verdict"], "ok")
        self.assertEqual(res["objects_checked"], 0)

    def test_no_live_session_honest_refusal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp-snegopat-empty-") as d:
            tk2 = Toolkit(Config(root=Path(d)))
            with self.assertRaises(Exception) as cm:
                spec_check.verify_claims(tk2, None,
                                         [{"text": "х", "objects": ["Справочник.Х"]}])
            self.assertIn("живых сессий нет", str(cm.exception))


class TestSpecCheckCli(TestSpecCheckBase):
    def _write_spec(self, claims, wrapper=False) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        data = {"claims": claims} if wrapper else claims
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return path

    def _run_main(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = spec_check.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def setUp(self) -> None:
        super().setUp()
        self._orig_make = spec_check._make_toolkit
        spec_check._make_toolkit = lambda: self.tk  # type: ignore[assignment]

    def tearDown(self) -> None:
        spec_check._make_toolkit = self._orig_make  # type: ignore[assignment]
        super().tearDown()

    def test_cli_rc0_all_ok(self) -> None:
        spec_path = self._write_spec(
            [{"text": "Только существующие", "objects": ["Документ.ЗаказКлиента"]}])
        try:
            rc, out, err = self._run_main(["--spec", spec_path])
        finally:
            os.unlink(spec_path)
        self.assertEqual((rc, err), (0, ""))
        res = json.loads(out)
        self.assertEqual(res["verdicts"][0]["verdict"], "ok")
        self.assertEqual(res["objects_checked"], 1)
        self.assertIn("ok: 1", res["summary"])

    def test_cli_dict_wrapper_accepted(self) -> None:
        spec_path = self._write_spec(
            [{"text": "Обёртка", "objects": ["Документ.ЗаказКлиента"]}],
            wrapper=True)
        try:
            rc, out, _ = self._run_main(["--spec", spec_path])
        finally:
            os.unlink(spec_path)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["objects_checked"], 1)

    def test_cli_rc1_missing(self) -> None:
        spec_path = self._write_spec(
            [{"text": "Нет объекта", "objects": ["Справочник.НеСуществует"]}])
        try:
            rc, out, _ = self._run_main(["--spec", spec_path])
        finally:
            os.unlink(spec_path)
        self.assertEqual(rc, 1)
        res = json.loads(out)
        self.assertEqual(res["verdicts"][0]["verdict"], "missing_objects")

    def test_cli_rc2_bad_spec(self) -> None:
        rc, _, err = self._run_main(["--spec", "Y:\\нет-такого-файла.json"])
        self.assertEqual(rc, 2)
        self.assertIn("spec_check", err)

    def test_cli_rc2_invalid_json(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{не json")
        try:
            rc, _, err = self._run_main(["--spec", path])
        finally:
            os.unlink(path)
        self.assertEqual(rc, 2)
        self.assertIn("spec_check", err)

    def test_cli_rc2_bad_spec_shape(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"unexpected": 1}')
        try:
            rc, _, err = self._run_main(["--spec", path])
        finally:
            os.unlink(path)
        self.assertEqual(rc, 2)

    def test_cli_rc2_bridge_unreachable(self) -> None:
        spec_check._make_toolkit = lambda: Toolkit(Config(root=Path(self.tmp.name) / "empty"))  # type: ignore[assignment]
        spec_path = self._write_spec([{"text": "х", "objects": ["Справочник.Договоры"]}])
        try:
            rc, _, err = self._run_main(["--spec", spec_path])
        finally:
            os.unlink(spec_path)
        self.assertEqual(rc, 2)
        self.assertIn("живых сессий нет", err)


if __name__ == "__main__":
    unittest.main()
