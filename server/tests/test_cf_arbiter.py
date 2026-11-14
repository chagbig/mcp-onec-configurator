# -*- coding: utf-8 -*-
"""Тесты IDEAS №12 (cf_arbiter): настройки MergeCfg, кандидаты recdiff, конвейер
на синтетической цепочке. Реальный 1cv8 в тестах НЕ запускается — шаги b/c/d
конвейера подменяются injectable-функциями (образец — CloseSequence в
test_enterprise_smoke), верификация идёт по РЕАЛЬНОМУ коду cf_inspect/cf_dump_json
на синтетических cf (фабрика блочных контейнеров — как в test_cf_dump_json).

Прогон: python -m unittest discover -s tests -p "test_cf_arbiter.py" (из server).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parent),
           str(Path(__file__).resolve().parents[1] / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cf_arbiter import (ArbiterError, arbiter_run, build_merge_settings,  # noqa: E402
                        check_objects, main, parse_recdiff_candidates,
                        verify_objects)

# ---- фабрика синтетического cf (формат cf_inspect, как в test_cf_dump_json) ----

OUTER_HEAD = (b"\xff\xff\xff\x7f\x00\x02\x00\x00\x05\x00\x00\x00"
              b"\x00\x00\x00\x00")
NESTED_HEAD = (b"\xff" * 8 + b"\x00\x02\x00\x00" + b"\x0b\x00\x00\x00"
               + b"\x00\x00\x00")
ROOT_OUTER_DATA = (b"\xef\xbb\xbf{2,"
                   b"ec5e5881-402b-445f-93d4-d908643416c2,"
                   b"gXib41jjasl9tXb1ePBV460fKXBALP//uFdPTn6i6hYi401V9u+LavTcnbCHOuIN"
                   b"\r\r\nvXO6YGsBJ/xk9bZfMWeZWg==}")
VERSION_DATA = "\ufeff{\r\n{216,0,\r\n{80327,0}\r\n}\r\n}".encode("utf-8")
VERSIONS_DATA = ("\ufeff{1,127,\"\",af5576dd-c993-4a31-9233-00c0e1599d90,"
                 "\"11111111-1111-1111-1111-111111111111\"}").encode("utf-8")
ROOT_NESTED_DATA = b'\xef\xbb\xbf{2,\r\n{1,"8.3.27.2074"},gXib41jjasl9tXb1ePBV460fKXBALP//uFdPTn6i6hYi401V9u+LavTcnbCHOuIN}'

UUID_A = "aaaaaaaa-0000-0000-0000-000000000001"
UUID_B = "aaaaaaaa-0000-0000-0000-000000000002"
UUID_C = "aaaaaaaa-0000-0000-0000-000000000003"
UUID_D = "aaaaaaaa-0000-0000-0000-000000000004"
NAME_A = "Номенклатура"
NAME_B = "Контрагенты"
NAME_C = "ПоступлениеТоваров"
NAME_D = "Склады"


def _deflate(data: bytes) -> bytes:
    co = zlib.compressobj(level=6, wbits=-15)
    return co.compress(data) + co.flush()


def _page(n: int) -> int:
    return ((n + 511) // 512) * 512


def _desc(size: int, page: int, width: int) -> bytes:
    nxt = 0x7FFFFFFF if width == 8 else 0xFFFFFFFFFFFFFFFF
    return ("\r\n%0*x %0*x %0*x \r\n" % (width, size, width, page, width, nxt)
            ).encode("ascii")


def _block(size: int, page: int, region: bytes, width: int = 8) -> bytes:
    assert len(region) == page
    return _desc(size, page, width) + region


def _record(name: str, data: bytes, width: int = 8) -> bytes:
    hdr = bytes(16) + bytes(5) + name.encode("utf-16-le") + b"\x00\x00"
    page = _page(len(hdr))
    out = _block(len(hdr), page, hdr.ljust(page, b"\x00"), width)
    s = _deflate(data)
    page = _page(len(s))
    return out + _block(len(s), page, s.ljust(page, b"\x00"), width)


def _serialization(uuid: str, name: str) -> bytes:
    return ('\ufeff{1,0,' + uuid + '},"' + name + '",\r\n{4,\r\n'
            '{3,"ru","Объект арбитра"}\r\n}').encode("utf-8")


def _build_cf(path: Path, objects: list[tuple]) -> Path:
    """objects: [(uuid, имя, {суффикс: payload_text})]; bare-запись несёт сериализацию."""
    outer = b"".join([_record("root", ROOT_OUTER_DATA, 8),
                      _record("version", VERSION_DATA, 8),
                      _record("versions", VERSIONS_DATA, 8)])
    nested = [_record("root", ROOT_NESTED_DATA, 16),
              _record("version", VERSION_DATA, 16),
              _record("versions", VERSIONS_DATA, 16)]
    for uuid, name, parts in objects:
        nested.append(_record(uuid, _serialization(uuid, name), 16))
        for suffix, payload in sorted(parts.items()):
            nested.append(_record(uuid + suffix, payload.encode("utf-8"), 16))
    path.write_bytes(OUTER_HEAD + outer + NESTED_HEAD + b"".join(nested))
    return path


def _default_objects() -> list[tuple]:
    return [
        (UUID_A, NAME_A, {"": "данные объекта А: " + "X" * 60,
                          ".0": "табличная часть А: " + "Y" * 60}),
        (UUID_B, NAME_B, {"": "данные объекта Б: " + "Z" * 60}),
        (UUID_C, NAME_C, {"": "данные объекта В: " + "Q" * 60,
                          ".0": "модуль объекта В: " + "M" * 60,
                          ".1": "макет В: " + "T" * 60}),
    ]


_OBJECTS = ["Справочник." + NAME_A, "Документ." + NAME_B, "Документ." + NAME_C]


class _TmpDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addClassCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)


class TestBuildMergeSettings(unittest.TestCase):
    NS = "{http://v8.1c.ru/8.3/config/merge/settings}"

    def test_structure(self):
        xml = build_merge_settings(["Документ.X", "Справочник.Y"])
        root = ET.fromstring(xml)
        self.assertEqual(root.tag, self.NS + "Settings")
        self.assertEqual(root.get("version"), "1.2")
        params = root.find(self.NS + "Parameters")
        self.assertEqual([p.tag.replace(self.NS, "") for p in params],
                         ["ConfigurationsRelation", "AllowMainConfigurationObjectDeletion",
                          "CopyObjectsMode"])
        self.assertEqual(params.find(self.NS + "ConfigurationsRelation").text,
                         "ConfigurationsNotRelated")
        objs = root.find(self.NS + "Objects")
        # первая запись — Configuration DoNotMerge (иначе всё неперечисленное затирается)
        conf = objs[0]
        self.assertEqual(conf.tag, self.NS + "Configuration")
        self.assertEqual(conf.find(self.NS + "MergeRule").text, "DoNotMerge")
        objects = [o for o in objs if o.tag == self.NS + "Object"]
        self.assertEqual([o.get("fullNameInSecondConfiguration") for o in objects],
                         ["Документ.X", "Справочник.Y"])
        for o in objects:
            self.assertEqual(o.find(self.NS + "MergeRule").text,
                             "GetFromSecondConfiguration")

    def test_version_lowercase_in_raw_text(self):
        xml = build_merge_settings(["Документ.X"])
        self.assertIn('version="1.2"', xml)
        # ловушка П4: атрибут Version с большой буквы платформой не принимается
        # (platformVersion с большой буквой внутри — легальный, как в эталоне)
        root = ET.fromstring(xml)
        self.assertIn("version", root.attrib)
        self.assertNotIn("Version", root.attrib)

    def test_escaping(self):
        xml = build_merge_settings(["Справочник.О\"Б"])
        root = ET.fromstring(xml)  # атрибут экранирован и разбирается обратно
        obj = [o for o in root.find(self.NS + "Objects")
               if o.tag == self.NS + "Object"][0]
        self.assertEqual(obj.get("fullNameInSecondConfiguration"), 'Справочник.О"Б')

    def test_empty_refused(self):
        with self.assertRaises(ArbiterError):
            build_merge_settings([])

    def test_duplicates_refused(self):
        with self.assertRaises(ArbiterError):
            build_merge_settings(["Документ.X", "Документ.X"])


class TestCheckObjects(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(check_objects(["Справочник.X"]), ["Справочник.X"])

    def test_subordinate_refused(self):
        # ловушка cf_diff №12: подчинённое имя — гарантированный ложный FAIL
        with self.assertRaises(ArbiterError) as cm:
            check_objects(["Документ.X.Реквизит.Y"])
        self.assertIn("Вид.Имя", str(cm.exception))

    def test_bare_name_refused(self):
        with self.assertRaises(ArbiterError):
            check_objects(["Номенклатура"])

    def test_unknown_kind_refused(self):
        with self.assertRaises(ArbiterError) as cm:
            check_objects(["СправочникПоставщиков.X"])
        self.assertIn("неизвестный вид", str(cm.exception))

    def test_empty_refused(self):
        with self.assertRaises(ArbiterError):
            check_objects([])


class TestParseRecdiffCandidates(_TmpDirTestCase):
    def _write(self, payload: dict) -> str:
        p = self.work / "recdiff.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def test_names_deduped_and_ordered(self):
        # дубли (uuid, суффикс) легальны — разворачиваются в ОДНО имя объекта
        p = self._write({
            "new": [UUID_A + ".0", UUID_A],
            "deleted": [UUID_B],
            "changed": [UUID_C, UUID_C + ".0", UUID_C + ".2"],
            "named": [{"record": UUID_A, "name": NAME_A},
                      {"record": UUID_B, "name": NAME_B},
                      {"record": UUID_C, "name": NAME_C}],
        })
        self.assertEqual(parse_recdiff_candidates(p, max_n=10),
                         [NAME_A, NAME_B, NAME_C])

    def test_max_n_cap(self):
        p = self._write({
            "new": [UUID_A, UUID_B, UUID_C],
            "named": [{"record": UUID_A, "name": NAME_A},
                      {"record": UUID_B, "name": NAME_B},
                      {"record": UUID_C, "name": NAME_C}],
        })
        self.assertEqual(parse_recdiff_candidates(p, max_n=2), [NAME_A, NAME_B])

    def test_unresolvable_skipped(self):
        # запись без имени в сериализациях пропускается (и не считается кандидатом)
        p = self._write({
            "new": [UUID_D, UUID_A],
            "named": [{"record": UUID_A, "name": NAME_A}],
        })
        self.assertEqual(parse_recdiff_candidates(p, max_n=10), [NAME_A])

    def test_sibling_lookup(self):
        # имя найдено для базового uuid, запись — с суффиксом
        p = self._write({
            "changed": [UUID_C + ".0"],
            "named": [{"record": UUID_C, "name": NAME_C}],
        })
        self.assertEqual(parse_recdiff_candidates(p, max_n=5), [NAME_C])

    def test_service_names_never_returned(self):
        p = self._write({
            "new": ["root", UUID_A],
            "named": [{"record": "root", "name": "root"},
                      {"record": UUID_A, "name": NAME_A}],
        })
        self.assertEqual(parse_recdiff_candidates(p, max_n=10), [NAME_A])

    def test_bad_json_raises(self):
        p = self.work / "bad.json"
        p.write_text("{не-json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            parse_recdiff_candidates(str(p), max_n=5)


class TestVerifyObjects(_TmpDirTestCase):
    def setUp(self):
        super().setUp()
        self.cf2 = _build_cf(self.work / "cf2.cf", _default_objects())

    def test_identical(self):
        result = _build_cf(self.work / "result.cf", _default_objects())
        verdicts = verify_objects(str(result), str(self.cf2), _OBJECTS)
        self.assertEqual([v["verdict"] for v in verdicts], ["identical"] * 3)
        self.assertEqual([v["object"] for v in verdicts], _OBJECTS)

    def test_mixed_verdicts(self):
        mutated = _default_objects()
        mutated[0] = (UUID_A, NAME_A,
                      {"": "данные объекта А: " + "X" * 60,
                       ".0": "ИЗМЕНЁННАЯ табличная часть А: " + "Y" * 40})  # payload другой
        del mutated[1]  # объект Б отсутствует в result → missing
        result = _build_cf(self.work / "result.cf", mutated + [
            (UUID_D, NAME_D, {"": "лишний объект D: " + "W" * 60}),  # нет в cf2
        ])
        verdicts = {v["object"]: v for v in
                    verify_objects(str(result), str(self.cf2), _OBJECTS)}
        self.assertEqual(verdicts[_OBJECTS[0]]["verdict"], "differs")
        self.assertIn(".0", verdicts[_OBJECTS[0]]["detail"])
        self.assertEqual(verdicts[_OBJECTS[1]]["verdict"], "missing")
        self.assertIn("result.cf", verdicts[_OBJECTS[1]]["detail"])
        self.assertEqual(verdicts[_OBJECTS[2]]["verdict"], "identical")
        # объект, которого нет в cf2 — сверять не с чем
        extra = verify_objects(str(result), str(self.cf2), ["Справочник." + NAME_D])
        self.assertEqual(extra[0]["verdict"], "missing")
        self.assertIn("cf2", extra[0]["detail"])

    def test_ambiguous_name_refused(self):
        result = _build_cf(self.work / "result.cf", _default_objects() + [
            (UUID_B[:-1] + "5", NAME_B, {"": "второй объект с тем же именем: " + "V" * 40}),
        ])
        with self.assertRaises(ArbiterError) as cm:
            verify_objects(str(result), str(self.cf2), _OBJECTS)
        self.assertIn("неоднозначно", str(cm.exception))


class TestArbiterRun(_TmpDirTestCase):
    """Полный цикл на синтетической цепочке: шаги подменены, верификация реальная."""

    def _fakes(self, result_builder):
        calls = []

        def init_base(cf1, base, work):
            calls.append(("init-base", cf1, base))
            Path(base).mkdir(parents=True, exist_ok=True)
            (Path(base) / "1Cv8.1CD").write_bytes(b"fake")

        def merge_cfg(base, cf2, settings, work):
            calls.append(("merge-cfg", base, cf2, settings))
            # settings.xml существует и несёт список объектов
            self.assertTrue(Path(settings).is_file())
            self.assertIn("fullNameInSecondConfiguration", Path(settings).read_text(encoding="utf-8"))

        def dump_cfg(base, result_cf, work):
            calls.append(("dump-cfg", base, result_cf))
            result_builder(Path(result_cf))

        return calls, init_base, merge_cfg, dump_cfg

    def test_full_cycle_identical_and_cleanup(self):
        cf2 = _build_cf(self.work / "cf2.cf", _default_objects())
        cf1 = shutil.copy2(cf2, self.work / "cf1.cf")  # содержимое не читается фейками
        calls, init, merge, dump = self._fakes(
            lambda out: shutil.copy2(cf2, out))
        rep = arbiter_run(str(cf1), str(cf2), _OBJECTS, work=str(self.work),
                          init_base=init, merge_cfg=merge, dump_cfg=dump)
        # последовательность: init → merge → dump → verify
        self.assertEqual([c[0] for c in calls],
                         ["init-base", "merge-cfg", "dump-cfg"])
        self.assertEqual(rep["steps"], ["init-base", "merge-cfg", "dump-cfg", "verify"])
        # merge получил cf2 и settings.xml из work
        self.assertEqual(calls[1][2], str(cf2))
        self.assertEqual(Path(calls[1][3]), self.work / "settings.xml")
        self.assertTrue(Path(rep["settings_file"]).is_file())
        self.assertTrue(Path(rep["result_cf"]).is_file())
        self.assertEqual([v["verdict"] for v in rep["verdicts"]], ["identical"] * 3)
        self.assertGreaterEqual(rep["elapsed_s"], 0.0)
        self.assertTrue(rep["steps"])
        # keep=False: временная база удалена, work остался
        self.assertFalse((self.work / "base-merge").exists())
        self.assertTrue(self.work.is_dir())

    def test_keep_leaves_base(self):
        cf2 = _build_cf(self.work / "cf2.cf", _default_objects())
        calls, init, merge, dump = self._fakes(
            lambda out: shutil.copy2(cf2, out))
        rep = arbiter_run(str(cf2), str(cf2), _OBJECTS[:1], work=str(self.work),
                          keep=True, init_base=init, merge_cfg=merge, dump_cfg=dump)
        self.assertTrue(rep["kept_base"])
        self.assertTrue((self.work / "base-merge" / "1Cv8.1CD").is_file())

    def test_merge_failure_cleans_base(self):
        cf2 = _build_cf(self.work / "cf2.cf", _default_objects())

        def init_base(cf1, base, work):
            Path(base).mkdir(parents=True, exist_ok=True)

        def merge_cfg(base, cf2, settings, work):
            raise ArbiterError("DumpResult=1 (не 0)")

        with self.assertRaises(ArbiterError):
            arbiter_run(str(cf2), str(cf2), _OBJECTS[:1], work=str(self.work),
                        init_base=init_base, merge_cfg=merge_cfg)
        # аварийный выход всё равно убирает временную базу
        self.assertFalse((self.work / "base-merge").exists())

    def test_differs_verdict_propagates(self):
        cf2 = _build_cf(self.work / "cf2.cf", _default_objects())
        mutated = _default_objects()
        mutated[0] = (UUID_A, NAME_A,
                      {"": "данные объекта А: " + "X" * 60,
                       ".0": "ДРУГОЙ payload: " + "N" * 50})
        calls, init, merge, dump = self._fakes(
            lambda out: _build_cf(out, mutated))
        rep = arbiter_run(str(cf2), str(cf2), _OBJECTS, work=str(self.work),
                          init_base=init, merge_cfg=merge, dump_cfg=dump)
        verdicts = {v["object"]: v["verdict"] for v in rep["verdicts"]}
        self.assertEqual(verdicts[_OBJECTS[0]], "differs")
        self.assertEqual(verdicts[_OBJECTS[1]], "identical")

    def test_missing_cf_refused(self):
        with self.assertRaises(ArbiterError) as cm:
            arbiter_run(str(self.work / "нет.cf"), str(self.work / "нет.cf"),
                        _OBJECTS[:1])
        self.assertIn("не найден", str(cm.exception))

    def test_bad_objects_refused_before_any_step(self):
        cf2 = _build_cf(self.work / "cf2.cf", _default_objects())
        with self.assertRaises(ArbiterError):
            arbiter_run(str(cf2), str(cf2), ["Документ.X.Реквизит.Y"])
        # временный work создан, но settings.xml не появился бы без валидации —
        # проверяем, что отказ не оставил настроек для мусорного списка
        self.assertFalse(list(self.work.glob("*/settings.xml")))


class TestMain(_TmpDirTestCase):
    def _cf(self, name):
        return str(_build_cf(self.work / name, _default_objects()))

    @staticmethod
    def _fake_run(rep):
        def run_fn(cf1, cf2, objects, work="", keep=False):
            run_fn.objects = objects
            run_fn.work = work
            run_fn.keep = keep
            return rep
        return run_fn

    def test_objects_mode_ok(self):
        rep = {"verdicts": [{"object": "Справочник.X", "verdict": "identical", "detail": ""}],
               "settings_file": "s", "result_cf": "r", "work": "w",
               "elapsed_s": 0.1, "steps": ["init-base", "merge-cfg", "dump-cfg", "verify"],
               "kept_base": False}
        run_fn = self._fake_run(rep)
        out = StringIO()
        with redirect_stdout(out):
            rc = main(["--cf1", self._cf("cf1.cf"), "--cf2", self._cf("cf2.cf"),
                       "--objects", "Справочник.X,Документ.Y", "--work", str(self.work)],
                      run_fn=run_fn)
        self.assertEqual(rc, 0)
        self.assertEqual(run_fn.objects, ["Справочник.X", "Документ.Y"])
        self.assertEqual(run_fn.work, str(self.work))
        self.assertIn("ИДЕНТИЧНО", out.getvalue())
        self.assertIn("Справочник.X", out.getvalue())

    def test_verdicts_nonzero_rc(self):
        rep = {"verdicts": [{"object": "Справочник.X", "verdict": "differs", "detail": "хеш"},
                            {"object": "Справочник.Y", "verdict": "missing", "detail": "нет"}],
               "settings_file": "s", "result_cf": "r", "work": "w",
               "elapsed_s": 0.1, "steps": ["verify"], "kept_base": False}
        out = StringIO()
        with redirect_stdout(out):
            rc = main(["--cf1", self._cf("cf1.cf"), "--cf2", self._cf("cf2.cf"),
                       "--objects", "Справочник.X"], run_fn=self._fake_run(rep))
        self.assertEqual(rc, 1)
        self.assertIn("РАЗЛИЧИЕ", out.getvalue())
        self.assertIn("ОТСУТСТВУЕТ", out.getvalue())

    def test_error_rc2(self):
        def bad_run(cf1, cf2, objects, work="", keep=False):
            raise ArbiterError("DESIGNER merge: DumpResult=1")
        rc = main(["--cf1", self._cf("cf1.cf"), "--cf2", self._cf("cf2.cf"),
                   "--objects", "Справочник.X"], run_fn=bad_run)
        self.assertEqual(rc, 2)

    def test_no_source_of_objects_rc2(self):
        rc = main(["--cf1", self._cf("cf1.cf"), "--cf2", self._cf("cf2.cf")])
        self.assertEqual(rc, 2)

    def test_missing_cf_file_rc2(self):
        rc = main(["--cf1", str(self.work / "нет.cf"), "--cf2", self._cf("cf2.cf"),
                   "--objects", "Справочник.X"])
        self.assertEqual(rc, 2)

    def test_recdiff_bare_names_refused_rc2(self):
        recdot = self.work / "recdiff.json"
        recdot.write_text(json.dumps({
            "new": [UUID_A], "named": [{"record": UUID_A, "name": NAME_A}],
        }, ensure_ascii=False), encoding="utf-8")
        err, out = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(["--cf1", self._cf("cf1.cf"), "--cf2", self._cf("cf2.cf"),
                       "--recdiff", str(recdot)])
        self.assertEqual(rc, 2)
        self.assertIn("БЕЗ вида", err.getvalue())
        self.assertIn(NAME_A, out.getvalue())  # кандидаты напечатаны

    def test_recdiff_dotted_names_run(self):
        recdot = self.work / "recdiff.json"
        recdot.write_text(json.dumps({
            "new": [UUID_A],
            "named": [{"record": UUID_A, "name": "Справочник." + NAME_A}],
        }, ensure_ascii=False), encoding="utf-8")
        rep = {"verdicts": [{"object": "Справочник." + NAME_A,
                             "verdict": "identical", "detail": ""}],
               "settings_file": "s", "result_cf": "r", "work": "w",
               "elapsed_s": 0.1, "steps": ["verify"], "kept_base": False}
        run_fn = self._fake_run(rep)
        out = StringIO()
        with redirect_stdout(out):
            rc = main(["--cf1", self._cf("cf1.cf"), "--cf2", self._cf("cf2.cf"),
                       "--recdiff", str(recdot), "--max-n", "5"], run_fn=run_fn)
        self.assertEqual(rc, 0)
        self.assertEqual(run_fn.objects, ["Справочник." + NAME_A])


if __name__ == "__main__":
    unittest.main()
