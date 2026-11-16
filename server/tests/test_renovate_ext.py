# -*- coding: utf-8 -*-
"""Тесты оркестратора renovate_ext (идея #8 MVP): инъекция шагов (образец —
init_base/merge_cfg/dump_cfg в test_cf_arbiter.py), fail-fast, skipped-с-причиной
(семантика #431 — никаких молчаливых пропусков), вердикты, rc-коды CLI.
Реальный 1cv8 НЕ запускается: build_ext/arbiter — ПЛАТФОРМЕННЫЕ шаги, в тестах
подменяются fake-результатами через run_step; шаг recdiff дополнительно
проверяется НАСТОЯЩИМ subprocess'ом на синтетических cf (фабрика блочных
контейнеров — как в test_cf_dump_json/test_cf_arbiter), платформа ему не нужна.

Прогон: python -m unittest discover -s tests -p "test_renovate_ext.py" (из server).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parent),
           str(Path(__file__).resolve().parents[1] / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import renovate_ext  # noqa: E402
from renovate_ext import STEPS, RenovateError, main, pipeline  # noqa: E402

# фабрика синтетического cf — реальный код cf_inspect/cf_recdiff читает эти
# контейнеры (шаг recdiff без платформы); реальный 1cv8 не нужен
from test_cf_arbiter import (  # noqa: E402
    UUID_A, UUID_D, NAME_A, _build_cf, _default_objects,
)

NAME_D = "Склады"


def _mkcf(work: Path, name: str, objects=None) -> str:
    """Синтетический cf (фабрика test_cf_arbiter) или пустышка-байты —
    инъекция шагов содержимое не читает."""
    p = work / name
    if objects is None:
        p.write_bytes(b"renovate-fake-cf")
    else:
        _build_cf(p, objects)
    return str(p)


def _ok_fakes(work: Path, arbiter_rc: int = 0) -> dict:
    """Fake-результаты полностью зелёного прогона. cfe-артефакт — существующий
    файл (постусловие build_ext проверяется и на fake)."""
    cfe = work / "fake-src.cfe"
    cfe.write_bytes(b"CFE-FAKE")
    return {
        "recdiff": {"rc": 2, "counts": {"new": 1, "deleted": 0, "changed": 2,
                                        "recreated": 0},
                    "candidates": ["Номенклатура"], "records1": 10, "records2": 12},
        "diff": {"rc": 0, "counts": {"changed_files": 2, "new_files": 1,
                                     "deleted_files": 0, "new_objs": 1},
                 "transferable": 2, "non_transferable": 3,
                 "transfer": [{"path": "Documents/X/ObjectModule.bsl",
                               "action": "interceptor"}],
                 "manual": [{"path": "Documents/X/Forms/Form.xml", "class": "form"}]},
        "build_ext": {"rc": 0, "cfe": str(cfe), "vendor_ok": True, "inserted": 3},
        "arbiter": {"rc": arbiter_rc, "identical": 3, "differs": 0, "missing": 0},
    }


class _TmpDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addClassCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        # CI не имеет платформы — гейт build_ext/arbiter должен считать её
        # найденной (иначе fake-шаги превращаются в skipped и ok-путь падает;
        # ловушка: на машине с 1cv8 тест зелёный, на CI красный). Тесты,
        # проверяющие «нет платформы», подменяют _find_1cv8 сами (None).
        patcher = mock.patch.object(renovate_ext, "_find_1cv8",
                                    return_value="fake")
        patcher.start()
        self.addCleanup(patcher.stop)


class TestPipelineOkRun(_TmpDirTestCase):
    """Полный ok-прогон на инъекции: последовательность шагов, отчёты, артефакт."""

    def test_full_ok_run(self):
        fakes = _ok_fakes(self.work)
        cf1 = _mkcf(self.work, "cf1.cf")
        cf2 = _mkcf(self.work, "cf2.cf")
        rep = pipeline(cf1, cf2, work=str(self.work / "run"), run_step=fakes)
        # последовательность шагов и статусы
        self.assertEqual([e["step"] for e in rep["steps"]], list(STEPS))
        self.assertTrue(all(e["status"] == "ok" for e in rep["steps"]))
        self.assertTrue(all(e["attempts"] == 1 for e in rep["steps"]))
        self.assertTrue(all(e["detail"] for e in rep["steps"]))  # без пустых деталей
        # счётчики из fake дошли до результата
        self.assertEqual(rep["recdiff"]["counts"]["new"], 1)
        self.assertEqual(rep["diff"]["transferable"], 2)
        self.assertFalse(rep["arbiter"]["disputed"])
        # артефакт скопирован из fake в корень work
        self.assertEqual(rep["artifact"], str(self.work / "run" / "МсрДиф.cfe"))
        self.assertTrue((self.work / "run" / "МсрДиф.cfe").is_file())
        # отчёты json+md в work
        report_json = self.work / "run" / "report.json"
        report_md = self.work / "run" / "report.md"
        self.assertTrue(report_json.is_file())
        self.assertTrue(report_md.is_file())
        self.assertEqual(rep["report_json"], str(report_json))
        loaded = json.loads(report_json.read_text(encoding="utf-8"))
        self.assertEqual(loaded["verdict"]["code"], "ok")
        md = report_md.read_text(encoding="utf-8")
        self.assertIn("Renovate расширений", md)
        self.assertIn("build_ext", md)
        self.assertIn("МсрДиф.cfe", md)
        self.assertIn("TRANSFER:", md)   # переносимое из fake-диффа
        self.assertIn("MANUAL:", md)     # непереносимое
        self.assertIn("Номенклатура", md)  # кандидаты recdiff
        self.assertEqual(rep["verdict"]["code"], "ok")

    def test_no_silent_skips_details_present(self):
        # каждый шаг — включая skipped — несёт причину в detail (#431)
        fakes = _ok_fakes(self.work)
        rep = pipeline(_mkcf(self.work, "cf1.cf"), _mkcf(self.work, "cf2.cf"),
                       work=str(self.work / "run"), run_step=fakes)
        for e in rep["steps"]:
            self.assertTrue(e["detail"], "шаг %s без detail" % e["step"])


class TestPipelineCleanup(_TmpDirTestCase):
    def _prepare_heavy(self):
        for sub in ("cfdiff", "build_ext", "arbiter"):
            d = self.work / "run" / sub
            d.mkdir(parents=True, exist_ok=True)
            (d / "heavy.bin").write_bytes(b"x" * 10)

    def test_cleanup_keeps_reports_and_artifact(self):
        self._prepare_heavy()
        fakes = _ok_fakes(self.work)
        rep = pipeline(_mkcf(self.work, "cf1.cf"), _mkcf(self.work, "cf2.cf"),
                       work=str(self.work / "run"), run_step=fakes)
        run = self.work / "run"
        for sub in ("cfdiff", "build_ext", "arbiter"):
            self.assertFalse((run / sub).exists(), sub)
        self.assertIn("cfdiff", " ".join(rep["cleaned"]))
        # отчёты и артефакт живы
        self.assertTrue((run / "report.json").is_file())
        self.assertTrue((run / "report.md").is_file())
        self.assertTrue((run / "МсрДиф.cfe").is_file())

    def test_keep_true_preserves_heavy(self):
        self._prepare_heavy()
        fakes = _ok_fakes(self.work)
        pipeline(_mkcf(self.work, "cf1.cf"), _mkcf(self.work, "cf2.cf"),
                 work=str(self.work / "run"), keep=True, run_step=fakes)
        for sub in ("cfdiff", "build_ext", "arbiter"):
            self.assertTrue((self.work / "run" / sub / "heavy.bin").is_file(), sub)


class TestPipelineFailFast(_TmpDirTestCase):
    """failed шаг останавливает пайплайн; отчёт пишется; cleanup не выполняется."""

    def test_diff_failed_stops_pipeline(self):
        fakes = _ok_fakes(self.work)
        fakes["diff"] = {"rc": 1, "error": "cf_diff: mockup_init cf1: rc=1"}
        self._prepare = (self.work / "run" / "cfdiff")
        self._prepare.mkdir(parents=True)
        (self._prepare / "evidence.log").write_text("улики", encoding="utf-8")
        rep = pipeline(_mkcf(self.work, "cf1.cf"), _mkcf(self.work, "cf2.cf"),
                       work=str(self.work / "run"), run_step=fakes)
        statuses = [e["status"] for e in rep["steps"]]
        self.assertEqual(statuses, ["ok", "failed", "skipped", "skipped"])
        # build_ext и arbiter НЕ выполнялись (не fake-результаты)
        self.assertIsNone(rep["build_ext"])
        self.assertIsNone(rep["arbiter"])
        self.assertIsNone(rep["artifact"])
        # skipped с причиной (никаких молчаливых пропусков)
        for e in rep["steps"][2:]:
            self.assertIn("failed-шагом 'diff'", e["detail"])
            self.assertEqual(e["attempts"], 0)
        # last_error донесён
        self.assertIn("mockup_init", rep["steps"][1]["last_error"])
        # вердикт failed, отчёт написан, cleanup отложен (улики живы)
        self.assertEqual(rep["verdict"]["code"], "failed")
        loaded = json.loads((self.work / "run" / "report.json").read_text(encoding="utf-8"))
        self.assertEqual([e["status"] for e in loaded["steps"]], statuses)
        self.assertTrue((self.work / "run" / "cfdiff" / "evidence.log").is_file())
        self.assertEqual(rep["cleaned"], [])

    def test_recdiff_failed_stops_everything(self):
        fakes = _ok_fakes(self.work)
        fakes["recdiff"] = {"rc": 1, "error": "cf_recdiff: ValueError"}
        rep = pipeline(_mkcf(self.work, "cf1.cf"), _mkcf(self.work, "cf2.cf"),
                       work=str(self.work / "run"), run_step=fakes)
        self.assertEqual([e["status"] for e in rep["steps"]],
                         ["failed", "skipped", "skipped", "skipped"])
        self.assertIsNone(rep["diff"])
        self.assertEqual(rep["verdict"]["code"], "failed")


class TestPipelineSkipped(_TmpDirTestCase):
    def test_build_ext_and_arbiter_skipped_without_platform(self):
        fakes = _ok_fakes(self.work)
        del fakes["build_ext"]   # реальный путь → проверка платформы
        del fakes["arbiter"]
        with mock.patch.object(renovate_ext, "_find_1cv8", return_value=None):
            rep = pipeline(_mkcf(self.work, "cf1.cf"), _mkcf(self.work, "cf2.cf"),
                           work=str(self.work / "run"), run_step=fakes)
        statuses = {e["step"]: e["status"] for e in rep["steps"]}
        self.assertEqual(statuses, {"recdiff": "ok", "diff": "ok",
                                    "build_ext": "skipped", "arbiter": "skipped"})
        # skipped с причиной «нет платформы», НЕ failed
        be = rep["build_ext"]
        self.assertEqual(be["status"], "skipped")
        self.assertIn("платформа", be["detail"])
        self.assertIn("1cv8.exe", be["detail"])
        self.assertIsNone(be["last_error"])
        self.assertIn("нет платформы", rep["arbiter"]["detail"])
        # нет платформы — нет артефакта; вердикт partial (не failed)
        self.assertIsNone(rep["artifact"])
        self.assertEqual(rep["verdict"]["code"], "partial")

    def test_identical_short_circuit(self):
        fakes = _ok_fakes(self.work)
        fakes["recdiff"] = {"rc": 0, "counts": {"new": 0, "deleted": 0, "changed": 0,
                                                "recreated": 0}}
        rep = pipeline(_mkcf(self.work, "cf1.cf"), _mkcf(self.work, "cf2.cf"),
                       work=str(self.work / "run"), run_step=fakes)
        statuses = {e["step"]: e["status"] for e in rep["steps"]}
        self.assertEqual(statuses["recdiff"], "ok")
        for step in ("diff", "build_ext", "arbiter"):
            self.assertEqual(statuses[step], "skipped", step)
            self.assertIn("cf идентичны", dict((e["step"], e["detail"])
                                               for e in rep["steps"])[step])
        # остальные шаги не выполнялись
        self.assertIsNone(rep["diff"])
        self.assertIsNone(rep["build_ext"])
        self.assertIsNone(rep["arbiter"])
        self.assertEqual(rep["verdict"]["code"], "no_changes")


class TestPipelineArbiterVerdicts(_TmpDirTestCase):
    def _run(self, fakes):
        with mock.patch.object(renovate_ext, "_find_1cv8",
                               return_value=r"C:\fake\bin\1cv8.exe"):
            return pipeline(_mkcf(self.work, "cf1.cf"), _mkcf(self.work, "cf2.cf"),
                            work=str(self.work / "run"), run_step=fakes)

    def test_arbiter_rc1_is_not_fail_disputed(self):
        fakes = _ok_fakes(self.work)
        fakes["arbiter"] = {"rc": 1, "identical": 2, "differs": 1, "missing": 1}
        rep = self._run(fakes)
        statuses = [e["status"] for e in rep["steps"]]
        self.assertEqual(statuses, ["ok", "ok", "ok", "ok"])  # rc=1 — НЕ failed
        self.assertTrue(rep["arbiter"]["disputed"])
        self.assertIn("спорные", rep["arbiter"]["detail"])
        self.assertEqual(rep["verdict"]["code"], "disputed")
        self.assertIn("требуют человека", rep["verdict"]["text"])
        # артефакт при этом построен
        self.assertTrue(rep["artifact"])
        md = (self.work / "run" / "report.md").read_text(encoding="utf-8")
        self.assertIn("требуется человек", md)

    def test_arbiter_rc2_skipped_with_note(self):
        fakes = _ok_fakes(self.work)
        fakes["arbiter"] = {"rc": 2, "error": "имена БЕЗ вида — MergeCfg не принимает"}
        rep = self._run(fakes)
        statuses = {e["step"]: e["status"] for e in rep["steps"]}
        self.assertEqual(statuses["arbiter"], "skipped")
        self.assertIn("отказ", rep["arbiter"]["detail"])
        self.assertIn("MergeCfg", rep["arbiter"]["detail"])
        # build_ext ok → вердикт ok, спора нет
        self.assertEqual(rep["verdict"]["code"], "ok")
        self.assertFalse(rep["arbiter"].get("disputed"))

    def test_no_candidates_skips_arbiter(self):
        fakes = _ok_fakes(self.work)
        fakes["recdiff"] = {"rc": 2, "counts": {"new": 0, "deleted": 0, "changed": 3,
                                                "recreated": 0}}
        del fakes["arbiter"]  # реальный путь: кандидаты есть (changed=3) → пойдёт?
        # changed>0 — кандидаты есть; для проверки «нет кандидатов» обнулим changed
        fakes["recdiff"]["counts"]["changed"] = 0
        rep = self._run(fakes)
        statuses = {e["step"]: e["status"] for e in rep["steps"]}
        self.assertEqual(statuses["arbiter"], "skipped")
        self.assertIn("нет кандидатов", rep["arbiter"]["detail"])


class TestPipelineGuards(_TmpDirTestCase):
    def test_missing_cf_raises(self):
        with self.assertRaises(RenovateError) as cm:
            pipeline(str(self.work / "нет.cf"), str(self.work / "нет.cf"),
                     work=str(self.work / "run"))
        self.assertIn("не найден", str(cm.exception))

    def test_build_ext_ok_without_cfe_file_is_failed(self):
        # постусловие проверяется и на fake: rc=0 без существующего cfe — failed
        fakes = _ok_fakes(self.work)
        fakes["build_ext"] = {"rc": 0, "cfe": str(self.work / "нет-такого.cfe"),
                              "vendor_ok": True}
        rep = pipeline(_mkcf(self.work, "cf1.cf"), _mkcf(self.work, "cf2.cf"),
                       work=str(self.work / "run"), run_step=fakes)
        statuses = {e["step"]: e["status"] for e in rep["steps"]}
        self.assertEqual(statuses["build_ext"], "failed")
        self.assertIn("постусловие", rep["steps"][2]["last_error"])
        self.assertEqual(rep["verdict"]["code"], "failed")

    def test_callable_fakes_supported(self):
        calls = []
        fakes = {k: (lambda k=k: (calls.append(k), _ok_fakes(self.work)[k])[1])
                 for k in STEPS}
        rep = pipeline(_mkcf(self.work, "cf1.cf"), _mkcf(self.work, "cf2.cf"),
                       work=str(self.work / "run"), run_step=fakes)
        self.assertEqual(calls, list(STEPS))
        self.assertTrue(all(e["status"] == "ok" for e in rep["steps"]))


class TestRealRecdiffStep(_TmpDirTestCase):
    """Шаг recdiff настоящим subprocess'ом на синтетических cf (cf_inspect):
    платформа не нужна; diff/build_ext/arbiter — инъекция."""

    def test_recdiff_real_on_synthetic_cf(self):
        cf1 = _mkcf(self.work, "cf1.cf", _default_objects())
        # cf2 = cf1 + новый объект D (uuid_D): recdiff обязан увидеть new=1.
        # D без payload-частей: bare-запись uuid_D — сериализация (запись payload
        # с суффиксом "" затёрла бы её в карте имя→запись скана cf_recdiff)
        objects_d = _default_objects() + [(UUID_D, NAME_D, {})]
        cf2 = _mkcf(self.work, "cf2.cf", objects_d)
        fakes = _ok_fakes(self.work)
        del fakes["recdiff"]
        rep = pipeline(cf1, cf2, work=str(self.work / "run"), run_step=fakes)
        self.assertEqual(rep["steps"][0]["step"], "recdiff")
        self.assertEqual(rep["steps"][0]["status"], "ok",
                         msg=json.dumps(rep["steps"][0], ensure_ascii=False))
        rec = rep["recdiff"]
        self.assertEqual(rec["counts"]["new"], 1)
        self.assertEqual(rec["counts"]["deleted"], 0)
        self.assertEqual(rec["counts"]["changed"], 0)
        self.assertFalse(rec["identical"])
        # имя нового объекта разрешено по сериализации
        self.assertIn(NAME_D, rec["candidates"])
        # отчёт recdiff.json реально лежит в work
        self.assertTrue((self.work / "run" / "recdiff.json").is_file())

    def test_recdiff_real_identical_pair(self):
        obj = _default_objects()
        cf = _mkcf(self.work, "same.cf", obj)
        fakes = _ok_fakes(self.work)
        del fakes["recdiff"]
        rep = pipeline(cf, cf, work=str(self.work / "run"), run_step=fakes)
        rec = rep["recdiff"]
        self.assertEqual(rec["status"], "ok",
                         msg=json.dumps(rec, ensure_ascii=False))
        self.assertTrue(rec["identical"])  # rc=0 и нулевые счётчики
        self.assertEqual(rec["counts"]["new"], 0)
        # короткое замыкание: остальные шаги не выполнялись
        self.assertIsNone(rep["diff"])
        self.assertEqual(rep["verdict"]["code"], "no_changes")


class TestMain(_TmpDirTestCase):
    def _cf(self, name):
        return _mkcf(self.work, name)

    def test_missing_args_rc2(self):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as cm:
                main([])
        self.assertEqual(cm.exception.code, 2)

    def test_missing_cf_file_rc2(self):
        err = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(err):
            rc = main([str(self.work / "нет.cf"), self._cf("cf2.cf"),
                       "--work", str(self.work / "run")])
        self.assertEqual(rc, 2)
        self.assertIn("не найден", err.getvalue())

    def test_ok_run_rc0(self):
        out = StringIO()
        with redirect_stdout(out):
            rc = main([self._cf("cf1.cf"), self._cf("cf2.cf"),
                       "--work", str(self.work / "run")], run_step=_ok_fakes(self.work))
        self.assertEqual(rc, 0)
        self.assertIn("вердикт: ok", out.getvalue())
        self.assertIn("МсрДиф.cfe", out.getvalue())
        self.assertIn("report.json", out.getvalue())

    def test_failed_run_rc1(self):
        fakes = _ok_fakes(self.work)
        fakes["diff"] = {"rc": 1, "error": "cf_diff: mockup_init cf1: rc=1"}
        out = StringIO()
        with redirect_stdout(out):
            rc = main([self._cf("cf1.cf"), self._cf("cf2.cf"),
                       "--work", str(self.work / "run")], run_step=fakes)
        self.assertEqual(rc, 1)
        self.assertIn("failed", out.getvalue())
        self.assertIn("diff", out.getvalue())

    def test_name_prefix_flow_to_artifact(self):
        fakes = _ok_fakes(self.work)
        out = StringIO()
        with redirect_stdout(out):
            rc = main([self._cf("cf1.cf"), self._cf("cf2.cf"),
                       "--name", "ТестРен", "--prefix", "Тст_", "--max-arbiter", "3",
                       "--work", str(self.work / "run")], run_step=fakes)
        self.assertEqual(rc, 0)
        # артефакт назван по --name
        self.assertTrue((self.work / "run" / "ТестРен.cfe").is_file())


if __name__ == "__main__":
    unittest.main()
