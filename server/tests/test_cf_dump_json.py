# -*- coding: utf-8 -*-
"""Тесты cf_dump_json: снимок-инвентарь cf в JSON (без платформы).

Прогон: python -m unittest discover -s tests -p "test_cf_dump_json.py"
(из каталога server).

Синтетический cf строится ФАБРИКОЙ _build_cf по формату cf_inspect (контейнер
v2): внешний контейнер (8-значные hex-поля) + вложенное хранилище
(16-значные), записи = raw-блок заголовка (16 байт + нули + имя utf-16le,
null-терминированное) + блок данных (raw deflate, wbits=-15). Подход тот же,
что в test_all.TestCfInspect (ручные блоки Container) и test_md_search
(синтетические блобы), но на уровне полного cf-файла.

Состав синтетического cf:
  внешний:  root, version, versions (служебные)
  вложенный: root (с версией "8.3.27.2074" для эвристики), version, versions
             (служебные) + объекты:
    11111111-…: bare + .0 (имя «СправочникТестовый» в обоих) + .1 (без имени)
    22222222-…: bare (имя «ДокументТестовый») + .0 (без пары имя-uuid)
    33333333-…: только .0, без пары имя-uuid → имени нет
    44444444-…: только bare-заголовок БЕЗ блока данных (пустая запись, size 0)
  Итого 13 записей: 6 служебных, 4 объекта.
"""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path

sys_path_1 = str(Path(__file__).resolve().parents[1])
sys_path_2 = str(Path(__file__).resolve().parent)
sys_path_3 = str(Path(__file__).resolve().parents[1] / "tools")
for _p in (sys_path_1, sys_path_2, sys_path_3):
    if _p not in __import__("sys").path:
        __import__("sys").path.insert(0, _p)

from cf_dump_json import dump, main  # noqa: E402

OUTER_HEAD = (b"\xff\xff\xff\x7f\x00\x02\x00\x00\x05\x00\x00\x00"
              b"\x00\x00\x00\x00")
NESTED_HEAD = (b"\xff" * 8 + b"\x00\x02\x00\x00" + b"\x0b\x00\x00\x00"
               + b"\x00\x00\x00")
NEXT8 = 0x7FFFFFFF
NEXT16 = 0xFFFFFFFFFFFFFFFF

OBJ1 = "11111111-1111-1111-1111-111111111111"
OBJ2 = "22222222-2222-2222-2222-222222222222"
OBJ3 = "33333333-3333-3333-3333-333333333333"
OBJ4 = "44444444-4444-4444-4444-444444444444"
NAME1 = "СправочникТестовый"
NAME2 = "ДокументТестовый"

OBJ1_BARE_DATA = ("\ufeff{1,0," + OBJ1 + '},"' + NAME1 + '",\r\n'
                  '{4,\r\n{3,"ru","Тестовый справочник"}\r\n}').encode("utf-8")
OBJ1_D0_DATA = ("\ufeff{1,0," + OBJ1 + '},"' + NAME1 + '",\r\n'
                '{4,\r\n{0,0,\r\n{2,7,"ru","Код"}\r\n}\r\n}').encode("utf-8")
OBJ1_D1_DATA = (b"\xef\xbb\xbf"
                + "Процедура ПриСозданииЭлемента(Отказ)\r\n"
                  "КонецПроцедуры\r\n".encode("utf-8"))
OBJ2_BARE_DATA = ("\ufeff{1,0," + OBJ2 + '},"' + NAME2 + '",\r\n'
                  "{4,1}").encode("utf-8")
# достаточно длинный payload: короткий deflate-поток может случайно закончиться
# парой (ascii, 00) и быть принят cf_inspect._header_name за заголовок записи
OBJ2_D0_DATA = ('\ufeff{4,\r\n{0,0,\r\n{2,7,"ru","Номер"}\r\n}\r\n}'
                ).encode("utf-8")
OBJ3_D0_DATA = ('\ufeff{4,\r\n{0,0,\r\n{1,"ru","БезИмени"}\r\n}\r\n}'
                ).encode("utf-8")
ROOT_OUTER_DATA = (b"\xef\xbb\xbf{2,"
                   b"ec5e5881-402b-445f-93d4-d908643416c2,"
                   b"gXib41jjasl9tXb1ePBV460fKXBALP//uFdPTn6i6hYi401V9u+LavTcnbCHOuIN"
                   b"\r\r\nvXO6YGsBJ/xk9bZfMWeZWg==}")
VERSION_DATA = "\ufeff{\r\n{216,0,\r\n{80327,0}\r\n}\r\n}".encode("utf-8")
VERSIONS_DATA = ("\ufeff{1,127,\"\",af5576dd-c993-4a31-9233-00c0e1599d90,"
                 + '"' + OBJ1 + '",'
                 + '"' + OBJ1 + '.0"}').encode("utf-8")
# вложенный root: эвристика версии — числовая строка N.N.N(.N) в payload-е
ROOT_NESTED_DATA = b'\xef\xbb\xbf{2,\r\n{1,"8.3.27.2074"},gXib41jjasl9tXb1ePBV460fKXBALP//uFdPTn6i6hYi401V9u+LavTcnbCHOuIN}'


def _deflate(data: bytes) -> bytes:
    co = zlib.compressobj(level=6, wbits=-15)
    return co.compress(data) + co.flush()


def _page(n: int) -> int:
    return ((n + 511) // 512) * 512


def _desc(size: int, page: int, width: int) -> bytes:
    nxt = NEXT8 if width == 8 else NEXT16
    return ("\r\n%0*x %0*x %0*x \r\n" % (width, size, width, page, width, nxt)
            ).encode("ascii")


def _block(size: int, page: int, region: bytes, width: int = 8) -> bytes:
    """Блок: заголовок-дескриптор + регион ровно page байт (хвост — нули)."""
    assert len(region) == page
    return _desc(size, page, width) + region


def _record(name: str, data: bytes | None, width: int = 8) -> bytes:
    """Запись контейнера: raw-блок заголовка (+ блок данных raw deflate).

    data=None — пустая запись (заголовок без данных, size 0), как «пустые»
    записи в реальных cf (cf_recdiff называет их при триаже рекреации).
    """
    hdr = bytes(16) + bytes(5) + name.encode("utf-16-le") + b"\x00\x00"
    page = _page(len(hdr))
    out = _block(len(hdr), page, hdr.ljust(page, b"\x00"), width)
    if data is not None:
        s = _deflate(data)
        page = _page(len(s))
        out += _block(len(s), page, s.ljust(page, b"\x00"), width)
    return out


def _build_cf(path: Path) -> Path:
    """Синтетический cf: внешний контейнер (служебные) + вложенное хранилище
    (служебные + объекты). Дескрипторы: внешний 8-hex, вложенный 16-hex —
    cf_inspect определяет ширину по первому блоку контейнера."""
    outer = b"".join([
        _record("root", ROOT_OUTER_DATA, 8),
        _record("version", VERSION_DATA, 8),
        _record("versions", VERSIONS_DATA, 8),
    ])
    nested = b"".join([
        _record("root", ROOT_NESTED_DATA, 16),
        _record("version", VERSION_DATA, 16),
        _record("versions", VERSIONS_DATA, 16),
        _record(OBJ1, OBJ1_BARE_DATA, 16),
        _record(OBJ1 + ".0", OBJ1_D0_DATA, 16),
        _record(OBJ1 + ".1", OBJ1_D1_DATA, 16),
        _record(OBJ2, OBJ2_BARE_DATA, 16),
        _record(OBJ2 + ".0", OBJ2_D0_DATA, 16),
        _record(OBJ3 + ".0", OBJ3_D0_DATA, 16),
        _record(OBJ4, None, 16),
    ])
    path.write_bytes(OUTER_HEAD + outer + NESTED_HEAD + nested)
    return path


class TestDumpSynthetic(unittest.TestCase):
    """dump() на синтетическом cf: структура, группировка, служебный подсчёт."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.cf = _build_cf(Path(cls._tmp.name) / "synthetic.cf")

    def test_structure_and_grouping(self):
        """(а) группировка по uuid: suffix, размер из заголовка, имена."""
        res = dump(str(self.cf))
        self.assertEqual(res["records_total"], 13)
        self.assertEqual(res["service_entries"], 6)
        self.assertEqual(len(res["objects"]), 4)
        uuids = {o["uuid"] for o in res["objects"]}
        self.assertEqual(uuids, {OBJ1, OBJ2, OBJ3, OBJ4})
        by_uuid = {o["uuid"]: o for o in res["objects"]}
        # имя распознано из bare- и .0-сериализаций
        self.assertEqual(by_uuid[OBJ1]["name"], NAME1)
        self.assertEqual(by_uuid[OBJ2]["name"], NAME2)
        # без пары имя-uuid в своих записях — имени нет (и это не ошибка)
        self.assertNotIn("name", by_uuid[OBJ3])
        self.assertNotIn("name", by_uuid[OBJ4])
        # записи объекта: суффиксы и размеры
        self.assertEqual([e["suffix"] for e in by_uuid[OBJ1]["entries"]],
                         ["", ".0", ".1"])
        self.assertEqual([e["size"] for e in by_uuid[OBJ1]["entries"]],
                         [len(_deflate(OBJ1_BARE_DATA)),
                          len(_deflate(OBJ1_D0_DATA)),
                          len(_deflate(OBJ1_D1_DATA))])
        self.assertEqual(by_uuid[OBJ3]["entries"],
                         [{"suffix": ".0", "size": len(_deflate(OBJ3_D0_DATA))}])
        # пустая запись: заголовок без блока данных → size 0
        self.assertEqual(by_uuid[OBJ4]["entries"],
                         [{"suffix": "", "size": 0}])

    def test_sorting(self):
        """(б) объекты отсортированы по name/uuid; безымянные — по uuid."""
        res = dump(str(self.cf))
        keys = [(o.get("name") or o["uuid"], o["uuid"]) for o in res["objects"]]
        self.assertEqual(keys, sorted(keys))
        # конкретный порядок: ascii-uuid (3…) < кириллица (Д… < С…)
        self.assertEqual([o.get("name") or o["uuid"][:1] for o in res["objects"]],
                         ["3", "4", NAME2, NAME1])

    def test_no_hash_by_default(self):
        res = dump(str(self.cf))
        for o in res["objects"]:
            for e in o["entries"]:
                self.assertNotIn("hash", e)

    def test_version_heuristic(self):
        """(в) версия-эвристика из root-записи вложенного контейнера."""
        res = dump(str(self.cf))
        self.assertEqual(res["version"], "8.3.27.2074")

    def test_note_honest_limitation(self):
        """(г) честное ограничение: структура реквизитов недоступна."""
        res = dump(str(self.cf))
        self.assertIn("РЕКВИЗИТОВ", res["note"].upper())
        self.assertEqual(res["file"], str(self.cf.resolve()))
        self.assertTrue(res["generated"])

    def test_service_records_not_in_objects(self):
        """Служебные записи root/version/versions на ОБОИХ уровнях — в
        service_entries, в objects не попадают."""
        res = dump(str(self.cf))
        self.assertEqual(res["service_entries"], 6)
        names = {o["uuid"] for o in res["objects"]}
        self.assertNotIn("root", names)
        self.assertEqual(res["records_total"],
                         res["service_entries"]
                         + sum(len(o["entries"]) for o in res["objects"]))


class TestDumpHash(unittest.TestCase):
    """use_hash=True: sha1 payload-ов (полное чтение только в этом режиме)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.cf = _build_cf(Path(cls._tmp.name) / "synthetic.cf")

    def test_hashes_are_sha1_of_payload(self):
        res = dump(str(self.cf), use_hash=True)
        by_uuid = {o["uuid"]: o for o in res["objects"]}
        entries = {e["suffix"]: e for e in by_uuid[OBJ1]["entries"]}
        self.assertEqual(entries[""]["hash"],
                         hashlib.sha1(OBJ1_BARE_DATA).hexdigest())
        self.assertEqual(entries[".0"]["hash"],
                         hashlib.sha1(OBJ1_D0_DATA).hexdigest())
        self.assertEqual(entries[".1"]["hash"],
                         hashlib.sha1(OBJ1_D1_DATA).hexdigest())
        # пустая запись: хеш пустого payload
        self.assertEqual(by_uuid[OBJ4]["entries"][0]["hash"],
                         hashlib.sha1(b"").hexdigest())

    def test_hash_format(self):
        res = dump(str(self.cf), use_hash=True)
        for o in res["objects"]:
            for e in o["entries"]:
                self.assertRegex(e["hash"], r"^[0-9a-f]{40}$")


class TestCli(unittest.TestCase):
    """CLI: JSON в tmp, rc=0, дефолт --out, --limit, ошибки → rc=2."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.cf = _build_cf(Path(cls._tmp.name) / "synthetic.cf")

    def test_cli_writes_json_rc0(self):
        out = Path(self._tmp.name) / "out" / "dump.json"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--cf", str(self.cf), "--out", str(out)])
        self.assertEqual(rc, 0)
        self.assertTrue(out.is_file())
        res = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(res["records_total"], 13)
        self.assertEqual(res["service_entries"], 6)
        self.assertEqual(len(res["objects"]), 4)
        self.assertIn("cf_dump_json: записей=13", buf.getvalue())

    def test_cli_default_out_next_to_cf(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--cf", str(self.cf)])
        self.assertEqual(rc, 0)
        default = Path(str(self.cf) + "-dump.json")
        self.assertTrue(default.is_file())
        res = json.loads(default.read_text(encoding="utf-8"))
        self.assertEqual(res["file"], str(self.cf.resolve()))

    def test_cli_hash(self):
        out = Path(self._tmp.name) / "dump-hash.json"
        with redirect_stdout(io.StringIO()):
            rc = main(["--cf", str(self.cf), "--out", str(out), "--hash"])
        self.assertEqual(rc, 0)
        res = json.loads(out.read_text(encoding="utf-8"))
        obj1 = next(o for o in res["objects"] if o["uuid"] == OBJ1)
        self.assertEqual(obj1["entries"][0]["hash"],
                         hashlib.sha1(OBJ1_BARE_DATA).hexdigest())

    def test_cli_limit(self):
        """--limit N ограничивает число объектов в JSON-отчёте."""
        out = Path(self._tmp.name) / "dump-limit.json"
        with redirect_stdout(io.StringIO()):
            rc = main(["--cf", str(self.cf), "--out", str(out), "--limit", "2"])
        self.assertEqual(rc, 0)
        res = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(res["objects"]), 2)

    def test_cli_missing_cf_rc2(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--cf", str(Path(self._tmp.name) / "нет.cf")])
        self.assertEqual(rc, 2)

    def test_cli_not_a_cf_rc2(self):
        bad = Path(self._tmp.name) / "bad.cf"
        bad.write_bytes(b"not a container" * 10)
        with redirect_stdout(io.StringIO()):
            rc = main(["--cf", str(bad)])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
