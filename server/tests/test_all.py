"""Тесты сервера mcp-snegopat на подставном мосте (unittest, без конфигуратора).

Прогон: python -m unittest discover -s server/tests -p "test_*.py" (из корня проекта)
или: python server/tests/test_all.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from fake_bridge import FakeBridge  # noqa: E402

def os_utime(p: Path, ts: float) -> None:
    import os as _os
    _os.utime(p, (ts, ts))

from mcp_snegopat.bridge import Bridge, BridgeError, NoSessionError, fnv1a  # noqa: E402
from mcp_snegopat.config import Config  # noqa: E402
from mcp_snegopat.server import Toolkit, build_server  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-snegopat-test-")
        self.root = Path(self.tmp.name)
        self.fake = FakeBridge(self.root, pid=self._free_pid()).start()
        self.cfg = Config(root=self.root, journal_path=self.root / "journal.jsonl")
        self.tk = Toolkit(self.cfg)
        # дождаться первой записи session.json потоком подставного моста
        for _ in range(300):
            if self.tk.bridge.live_sessions():
                break
            time.sleep(0.01)
        else:
            self.fail("подставной мост не поднялся за 3 с")

    def tearDown(self) -> None:
        self.fake.stop()
        self.tmp.cleanup()

    @staticmethod
    def _free_pid() -> int:
        # подставной мост живёт в текущем процессе, значит pid жив — как конфигуратор
        import os
        return os.getpid()


class TestConfig(unittest.TestCase):
    def test_defaults_safe(self) -> None:
        cfg = Config()
        self.assertFalse(cfg.allow_store)
        self.assertFalse(cfg.allow_update_db)
        self.assertEqual(cfg.own_bases, [])

    def test_load_from_file(self) -> None:
        from mcp_snegopat.config import load_config
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cfg.json"
            p.write_text(json.dumps({
                "root": str(Path(d)),
                "own_bases": ['File="C:\\b";'],
                "allow_update_db": True,
            }), encoding="utf-8")
            cfg = load_config(p)
            self.assertTrue(cfg.allow_update_db)
            self.assertEqual(cfg.own_bases, ['File="C:\\b";'])
            self.assertEqual(cfg.root, Path(d))

    def test_unknown_key_rejected(self) -> None:
        from mcp_snegopat.config import load_config
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cfg.json"
            p.write_text(json.dumps({"strange_key": 1}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(p)


class TestBridge(Base):
    def test_ping_roundtrip(self) -> None:
        pid = self.tk._pid(None)
        res = self.tk.bridge.call(pid, "ping")
        self.assertTrue(res["pong"])

    def test_platform_gate(self) -> None:
        """Непроверенная платформа — сессия не выбирается; флаг снимает запрет."""
        pid = self.tk._pid(None)
        self.fake.platform = "8.3.24.100"
        self.fake.heartbeat()
        with self.assertRaises(NoSessionError) as cm:
            self.tk.bridge.pick_session(pid)
        self.assertIn("8.3.27", str(cm.exception))
        self.cfg.allow_unverified_platform = True
        self.assertEqual(self.tk.bridge.pick_session(pid), pid)
        self.cfg.allow_unverified_platform = False
        self.fake.platform = "8.3.27.2074"
        self.fake.heartbeat()
        self.assertEqual(self.tk.bridge.pick_session(pid), pid)

    def test_error_op_raises_with_code(self) -> None:
        pid = self.tk._pid(None)
        with self.assertRaises(BridgeError) as cm:
            self.tk.bridge.call(pid, "error_op")
        self.assertEqual(cm.exception.code, "exception")

    def test_unknown_op(self) -> None:
        pid = self.tk._pid(None)
        with self.assertRaises(BridgeError) as cm:
            self.tk.bridge.call(pid, "no_such_op")
        self.assertEqual(cm.exception.code, "unknown_op")

    def test_timeout(self) -> None:
        pid = self.tk._pid(None)
        t0 = time.time()
        with self.assertRaises(BridgeError) as cm:
            self.tk.bridge.call(pid, "slow_op", timeout=0.3)
        self.assertEqual(cm.exception.code, "timeout")
        self.assertLess(time.time() - t0, 1.0)

    def test_dead_session_marked(self) -> None:
        # сессия с pid завершившегося процесса — мертва
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()
        dead_dir = self.root / "sessions" / str(proc.pid)
        (dead_dir / "req").mkdir(parents=True)
        (dead_dir / "session.json").write_text(
            json.dumps({"pid": proc.pid, "heartbeat": int(time.time() * 1000)}), encoding="utf-8")
        sess = {s.pid: s for s in self.tk.bridge.sessions()}
        self.assertIn(proc.pid, sess)
        self.assertFalse(sess[proc.pid].alive)
        self.assertTrue(sess[self.fake.pid].alive)

    def test_pick_session_missing_pid(self) -> None:
        with self.assertRaises(NoSessionError):
            self.tk.bridge.pick_session(999999)

    def test_no_sessions_empty_ok(self) -> None:
        # критерий этапа 3: ноль сессий — пусто без ошибки
        with tempfile.TemporaryDirectory() as d:
            empty = Bridge(Config(root=Path(d) / "nope"))
            self.assertEqual(empty.sessions(), [])
            tk = Toolkit(Config(root=Path(d) / "nope"))
            self.assertEqual(tk.list_sessions(), {"sessions": []})
            with self.assertRaises(NoSessionError):
                tk.bridge.pick_session(None)


class TestModules(Base):
    P = "ОбщийМодуль.Тест"
    TEXT = "Процедура Тест()\n\tВозврат;\nКонецПроцедуры\n"

    def _seed(self) -> str:
        self.fake.modules[(self.P, "Модуль")] = self.TEXT
        return fnv1a(self.TEXT)

    def test_get_set_roundtrip(self) -> None:
        h = self._seed()
        got = self.tk.module_get(path=self.P, prop="Модуль", with_text=True)
        self.assertEqual(got["hash"], h)
        self.assertEqual(got["text"], self.TEXT)
        new = "// правка\n" + self.TEXT
        res = self.tk.module_set(path=self.P, prop="Модуль", text=new, expected_hash=h)
        self.assertEqual(res["hash"], fnv1a(new))
        again = self.tk.module_get(path=self.P, prop="Модуль")
        self.assertEqual(again["hash"], fnv1a(new))

    def test_module_set_wrong_hash_refused(self) -> None:
        self._seed()
        with self.assertRaises(BridgeError) as cm:
            self.tk.module_set(path=self.P, prop="Модуль", text="x", expected_hash="deadbeef")
        self.assertIn("expected_hash", str(cm.exception))
        # текст не изменился
        self.assertEqual(self.fake.modules[(self.P, "Модуль")], self.TEXT)

    def test_module_patch_ok(self) -> None:
        h = self._seed()
        res = self.tk.module_patch(path=self.P, prop="Модуль",
                                   edits=[{"line": 1, "from": "Процедура Тест()",
                                           "to": "Процедура ТестИзменённая()"}],
                                   expected_hash=h)
        self.assertEqual(res["applied"], 1)
        self.assertIn("ТестИзменённая", self.fake.modules[(self.P, "Модуль")])

    def test_module_patch_wrong_line_refused(self) -> None:
        h = self._seed()
        with self.assertRaises(BridgeError):
            self.tk.module_patch(path=self.P, prop="Модуль",
                                 edits=[{"line": 1, "from": "не та строка", "to": "x"}],
                                 expected_hash=h)
        self.assertEqual(self.fake.modules[(self.P, "Модуль")], self.TEXT)


class TestAddDelete(Base):
    def test_add_child_sibling(self) -> None:
        self.fake.objects[""] = {"name": "Конфигурация", "id": "root", "cls": "Конфигурация"}
        res = self.tk.md_add_child(owner_path="Справочник.Товары", kind="Реквизиты",
                                   name="НовыйРеквизит", sibling="СтарыйРеквизит",
                                   comment="тест")
        self.assertTrue(res["renamed"])
        self.assertEqual(res["created"]["cls"], "Реквизит")
        ops = [op for op, _ in self.fake.args_seen]
        # фокус отдельным тиком перед нажатием (ловушка 1 этапа 7)
        self.assertLess(ops.index("tree_activate"), ops.index("md_add_child"))
        self.assertGreaterEqual(ops.count("active_view"), 1)

    def test_add_child_group_nav(self) -> None:
        self.fake.objects[""] = {"name": "Конфигурация", "id": "root", "cls": "Конфигурация"}
        res = self.tk.md_add_child(owner_path="", kind="Справочники", name="НовыйСправочник")
        add_args = [a for op, a in self.fake.args_seen if op == "md_add_child"][0]
        self.assertTrue(add_args["firstChild"])
        # Конфигурация -> Справочники = k 3: [+] + (вниз, свернуть)*3
        self.assertEqual(add_args["navKeys"], [0x6B, 0x28, 0x6D, 0x28, 0x6D, 0x28, 0x6D])
        self.assertEqual(res["created"]["cls"], "Справочник")

    def test_add_child_unknown_kind(self) -> None:
        with self.assertRaises(BridgeError) as cm:
            self.tk.md_add_child(owner_path="", kind="Чудеса", name="Х")
        self.assertEqual(cm.exception.code, "bad_args")

    def test_add_child_no_tree_focus(self) -> None:
        self.fake.focus_fail_times = 10
        with self.assertRaises(BridgeError) as cm:
            self.tk.md_add_child(owner_path="Справочник.Х", kind="Реквизиты", name="Н", sibling="С")
        self.assertEqual(cm.exception.code, "no_tree_focus")
        self.assertNotIn("md_add_child", [op for op, _ in self.fake.args_seen])

    def test_delete_requires_uuid(self) -> None:
        with self.assertRaises(BridgeError) as cm:
            self.tk.md_delete(path="Справочник.Х.Реквизит.У", uuid="")
        self.assertEqual(cm.exception.code, "bad_args")

    def test_first_ts_attribute_refused(self) -> None:
        """Первый реквизит ТЧ — явный отказ до дерева (26.10.2026)."""
        calls = {"orig": self.tk.bridge.call}

        def call(pid, op, args=None, timeout=None):
            if op == "md_object":
                return {"name": "Т", "id": "x2", "cls": "ТабличнаяЧасть"}
            if op == "md_children":
                return {"kind": "Реквизиты", "count": 0, "items": []}
            return calls["orig"](pid, op, args, timeout)

        self.tk.bridge.call = call
        try:
            with self.assertRaises(BridgeError) as cm:
                self.tk.md_add_child(owner_path="Документ.Д.ТабличнаяЧасть.Т",
                                     kind="Реквизиты", name="Н")
            self.assertIn("первый реквизит", str(cm.exception))
        finally:
            self.tk.bridge.call = calls["orig"]


class TestGating(Base):
    def test_update_db_cfg_forbidden_default(self) -> None:
        with self.assertRaises(BridgeError) as cm:
            self.tk.update_db_cfg()
        self.assertEqual(cm.exception.code, "forbidden")

    def test_update_db_cfg_allowed_passes_own_bases(self) -> None:
        self.cfg.allow_update_db = True
        self.cfg.own_bases = ['File="C:\\b";']
        self.tk.update_db_cfg()
        upd = [a for op, a in self.fake.args_seen if op == "update_db_cfg"]
        self.assertEqual(upd[0]["own_bases"], ['File="C:\\b";'])
        self.assertIn("реорганизация информации", upd[0]["allowedDialogs"])

    def test_update_db_cfg_own_bases_empty(self) -> None:
        self.cfg.allow_update_db = True
        with self.assertRaises(BridgeError) as cm:
            self.tk.update_db_cfg()
        self.assertEqual(cm.exception.code, "forbidden")

    def test_cfgstore_forbidden_default(self) -> None:
        with self.assertRaises(BridgeError):
            self.tk.cfgstore_capture(path="Справочник.Х")
        with self.assertRaises(BridgeError):
            self.tk.cfgstore_commit(path="Справочник.Х", comment="к")

    def test_cfgstore_commit_needs_comment(self) -> None:
        self.cfg.allow_store = True
        with self.assertRaises(BridgeError) as cm:
            self.tk.cfgstore_commit(path="Справочник.Х", comment="   ")
        self.assertEqual(cm.exception.code, "bad_args")

    def test_rename_requires_uuid(self) -> None:
        with self.assertRaises(BridgeError) as cm:
            self.tk.md_set_property(path="Справочник.Х", prop="Имя", value="НовоеИмя")
        self.assertEqual(cm.exception.code, "bad_args")

    def test_module_set_requires_hash(self) -> None:
        with self.assertRaises(BridgeError) as cm:
            self.tk.module_set(path="А", prop="Модуль", text="x")
        self.assertEqual(cm.exception.code, "bad_args")


class TestFormText(unittest.TestCase):
    """Шапка-нормализатор внутреннего текста формы (этап 8, §4.1)."""

    ACTIVE = "{-1,02023637-7868-4a5f-8576-835a76e0c9ba},0,0,0,9,"

    def test_normalize_active_element(self) -> None:
        from form_text import normalize_form_text, texts_equal
        a = self.ACTIVE + '"ФормаКоманднаяПанель",'
        b = self.ACTIVE + '"",'
        self.assertEqual(normalize_form_text(a), normalize_form_text(b))
        self.assertTrue(texts_equal(a, b))

    def test_normalize_keeps_other_fields(self) -> None:
        from form_text import normalize_form_text
        t = "{-1,02023637-7868-4a5f-8576-835a76e0c9ba},1,0,0,9," + '"ИмяЭлемента",'
        self.assertEqual(normalize_form_text(t), t)

    def test_texts_equal_eol(self) -> None:
        from form_text import texts_equal
        crlf = "{4," + chr(13) + chr(10) + "{50,{x}"
        cr = "{4," + chr(13) + "{50,{x}"
        self.assertTrue(texts_equal(crlf, cr))

    def test_normalize_real_shape(self) -> None:
        from form_text import texts_equal
        cf = self.ACTIVE + '"",'
        fs = self.ACTIVE + '"ФормаКоманднаяПанель",'
        self.assertTrue(texts_equal(cf, fs))


class TestCfInspect(unittest.TestCase):
    """cf_inspect: имена заголовков записей (без бинарных срабатываний)."""

    def test_header_name(self) -> None:
        import cf_inspect as ci
        name = "40916224-d255-42af-a76d-ff676ac881eb"
        payload = bytes(16) + bytes(5) + name.encode("utf-16-le") + bytes(4)
        self.assertEqual(ci._header_name(payload), name)

    def test_header_name_rejects_garbage(self) -> None:
        import cf_inspect as ci
        toc = b"/" + b"\r\n" + b"\x02\x00\x00" + b"\xae\x02\x00\x00" + b"\xff\xff\xff\x7f" * 3
        self.assertIsNone(ci._header_name(toc))

    def test_header_name_short(self) -> None:
        import cf_inspect as ci
        self.assertIsNone(ci._header_name(b"root\x00\x00"))

    def test_chained_toc_walked_sequentially(self) -> None:
        """TOC-страница с нетерминальным next не срывает обход (cf 2 ГБ, 28.10)."""
        import io
        import cf_inspect as ci
        name = "40916224-d255-42af-a76d-ff676ac881eb"
        hdr = bytes(16) + bytes(5) + name.encode("utf-16-le") + bytes(4)
        b1 = (b"\r\n" + b"00000020 00000020 00001000 " + b"\r\n" + b"X" * 32)
        d1 = b"\r\n" + ("%08x %08x %08x" % (len(hdr), 128, 0x7fffffff)).encode() + b" \r\n"
        buf = io.BytesIO()
        buf.write(ci.OUTER_MAGIC + bytes(10))
        buf.write(b1)
        buf.write(d1)
        buf.write(hdr + bytes(128 - len(hdr)))
        buf.write(b"\r\n" + b"00000008 00000008 7fffffff " + b"\r\n" + b"Y" * 8)
        c = ci.Container(buf, 16)
        out = []
        pending = None
        for blk in c.walk():
            pay, comp = c.payload(blk)
            nm = ci._header_name(pay) if not comp else None
            if nm:
                pending = nm
            elif pending:
                out.append(pending)
                pending = None
        self.assertEqual(out, [name])



class TestFormXmlCacheHelpers(unittest.TestCase):
    """Кэш М2: идентичность cf и маркер temp_read (form_xml)."""

    def test_cf_key_identity(self) -> None:
        import time as _t
        from form_xml import _cf_key
        with tempfile.TemporaryDirectory() as d:
            a = Path(d) / "a.cf"
            a.write_bytes(b"x" * 10)
            _t.sleep(0.02)
            b = Path(d) / "b.cf"
            b.write_bytes(b"x" * 10)
            self.assertEqual(_cf_key(a), _cf_key(a))
            self.assertNotEqual(_cf_key(a), _cf_key(b))

    def test_tread_marker_roundtrip(self) -> None:
        from form_xml import _read_tread_marker, _tread_reusable, _write_tread_marker
        with tempfile.TemporaryDirectory() as d:
            cf = Path(d) / "mcp-x.cf"
            cf.write_bytes(b"y" * 20)
            work = Path(d) / "work"
            work.mkdir()
            self.assertFalse(_tread_reusable(work, cf))  # маркера нет
            _write_tread_marker(work, cf)
            self.assertIsNotNone(_read_tread_marker(work))
            self.assertTrue(_tread_reusable(work, cf))
            # другой cf (размер) — маркер не подходит
            cf2 = Path(d) / "mcp-y.cf"
            cf2.write_bytes(b"y" * 21)
            self.assertFalse(_tread_reusable(work, cf2))


class TestFormXmlValidateAndGc(unittest.TestCase):
    """Валидация XML до цепочки и retention тяжёлых каталогов (form_xml)."""

    FORM_OK = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">'
               '<AutoCommandBar name="" id="-1"/></Form>')

    def test_validate_ok(self) -> None:
        from form_xml import _validate_form_xml
        _validate_form_xml(self.FORM_OK)

    def test_validate_broken(self) -> None:
        from form_xml import FormXmlError, _validate_form_xml
        with self.assertRaises(FormXmlError) as cm:
            _validate_form_xml(self.FORM_OK.replace("</Form>", "<Form>"))
        self.assertIn("не разбирается", str(cm.exception))

    def test_validate_wrong_root(self) -> None:
        from form_xml import FormXmlError, _validate_form_xml
        with self.assertRaises(FormXmlError) as cm:
            _validate_form_xml('<?xml version="1.0"?><Other/>')
        self.assertIn("корневой элемент", str(cm.exception))

    def test_gc_work_keeps_newest_and_excludes_current(self) -> None:
        import time as _t
        from form_xml import _gc_work
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old = []
            for i in range(4):
                p = root / ("w%d" % i)
                p.mkdir()
                (p / "f").write_text("x")
                old_t = _t.time() - 7200 - i
                os_utime(p, old_t)
                old.append(p)
            fresh = root / "fresh"
            fresh.mkdir()
            current = root / "current"
            current.mkdir()
            os_utime(current, _t.time() - 7200)
            removed = _gc_work(root, keep=2, min_age_s=3600, exclude=current)
            self.assertEqual(len(removed), 2)
            self.assertTrue(fresh.is_dir() and current.is_dir())
            self.assertEqual(len(list(root.iterdir())), 4)  # 2 старых + fresh + current

    def test_gc_arch_cfs(self) -> None:
        import time as _t
        from form_xml import _gc_arch_cfs
        with tempfile.TemporaryDirectory() as d:
            b = Path(d)
            f1 = b / "mcp-formxml-1.cf"
            f1.write_bytes(b"x")
            os_utime(f1, _t.time() - 7200)
            f2 = b / "mcp-formxml-2.cf"
            f2.write_bytes(b"x")
            other = b / "other.cf"
            other.write_bytes(b"x")
            os_utime(other, _t.time() - 7200)
            removed = _gc_arch_cfs(b, min_age_s=3600, extra_keep=f2)
            self.assertEqual(removed, ["mcp-formxml-1.cf"])
            self.assertTrue(f2.is_file() and other.is_file())



class TestFillShellAdopted(unittest.TestCase):
    """C1 ревью v1.0.0: Adopted-ветвь fill_shell (реквизиты ТЧ cf2 → расширение)."""

    HDR = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
           'xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
           'xmlns:v8="http://v8.1c.ru/8.1/data/core" '
           'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">')

    def _doc(self, ts_uuid, attr_uuid=None):
        attr = ""
        if attr_uuid:
            attr = ('<Attribute uuid="%s"><Properties><Name>Реквизит1</Name>'
                    '<Type><v8:Type>xs:string</v8:Type></Type></Properties></Attribute>'
                    % attr_uuid)
        return (self.HDR + '<Document uuid="11111111-1111-1111-1111-111111111111">'
                '<Properties><Name>Док</Name></Properties>'
                '<ChildObjects><TabularSection uuid="%s">'
                '<Properties><Name>Т</Name></Properties>'
                '<ChildObjects>%s</ChildObjects></TabularSection>'
                '</ChildObjects></Document></MetaDataObject>' % (ts_uuid, attr))

    def test_adopted_tch_with_new_attr(self) -> None:
        import tempfile
        from pathlib import Path as _P
        from build_ext import fill_shell
        with tempfile.TemporaryDirectory() as d:
            shell = _P(d) / "shell.xml"
            shell.write_text(self.HDR + '<Document uuid="aaaaaaaa-1111-1111-1111-111111111111">'
                             '<Properties><ObjectBelonging>Adopted</ObjectBelonging>'
                             '<Name>Док</Name><ExtendedConfigurationObject>'
                             '11111111-1111-1111-1111-111111111111</ExtendedConfigurationObject>'
                             '</Properties><ChildObjects/></Document></MetaDataObject>',
                             encoding="utf-8-sig")
            f1 = _P(d) / "c1.xml"
            f1.write_text(self._doc("22222222-2222-2222-2222-222222222222"),
                          encoding="utf-8-sig")
            f2 = _P(d) / "c2.xml"
            f2.write_text(self._doc("22222222-2222-2222-2222-222222222222",
                                    "33333333-3333-3333-3333-333333333333"),
                          encoding="utf-8-sig")
            r = fill_shell(shell, f1, f2, "Мср14_")
            self.assertEqual(r["added_tch"], 1)  # C1: ветвь жива
            self.assertEqual(r["added_attr"], 1)
            out = shell.read_text(encoding="utf-8-sig")
            self.assertIn("<ObjectBelonging>Adopted</ObjectBelonging>", out)
            self.assertIn("ExtendedConfigurationObject>22222222-2222-2222-2222-222222222222<", out)
            self.assertIn("Мср14_Реквизит1", out)

    def test_adopted_tch_without_new_attrs_skipped(self) -> None:
        import tempfile
        from pathlib import Path as _P
        from build_ext import fill_shell
        with tempfile.TemporaryDirectory() as d:
            shell = _P(d) / "shell.xml"
            shell.write_text(self.HDR + '<Document uuid="aaaaaaaa-1111-1111-1111-111111111111">'
                             '<Properties><ObjectBelonging>Adopted</ObjectBelonging>'
                             '<Name>Док</Name></Properties><ChildObjects/></Document>'
                             '</MetaDataObject>', encoding="utf-8-sig")
            f1 = _P(d) / "c1.xml"
            f1.write_text(self._doc("22222222-2222-2222-2222-222222222222"),
                          encoding="utf-8-sig")
            f2 = _P(d) / "c2.xml"
            f2.write_text(self._doc("22222222-2222-2222-2222-222222222222"),
                          encoding="utf-8-sig")
            r = fill_shell(shell, f1, f2, "Мср14_")
            self.assertEqual((r["added_tch"], r["added_attr"], r["inserted"]), (0, 0, 0))


class TestStage15Forms(unittest.TestCase):
    """Этап 15: fill_form — перенос изменённой формы как замещающей (Adopted)."""

    U_CF1 = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    U_EXT = "43e2f496-6d45-428c-b538-f6509f303265"

    WRAPPER = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
               '<Form uuid="%s"><Properties>'
               '<ObjectBelonging>Adopted</ObjectBelonging><Name>ФормаДокумента</Name>'
               '<ExtendedConfigurationObject>%s</ExtendedConfigurationObject>'
               '<FormType>Managed</FormType></Properties></Form></MetaDataObject>')

    CONTENT_V1 = ('<?xml version="1.0" encoding="UTF-8"?>'
                  '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20">'
                  '<AutoCommandBar name="" id="-1"><ChildItems/></AutoCommandBar>'
                  '<Attributes/></Form>')
    CONTENT_V2 = ('<?xml version="1.0" encoding="UTF-8"?>'
                  '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20">'
                  '<AutoCommandBar name="" id="-1"><ChildItems/></AutoCommandBar>'
                  '<Events><Event name="OnCreateAtServer">ПриСозданииНаСервере</Event></Events>'
                  '<Attributes/></Form>')
    MODULE = ("#Область ОбработчикиСобытийФормы\r\n\r\n&НаСервере\r\n"
              "Процедура ПриСозданииНаСервере(Отказ, СтандартнаяОбработка)\r\n"
              "\t// П15\r\nКонецПроцедуры\r\n\r\n#КонецОбласти\r\n")

    def _stand(self, d: Path, content: str = CONTENT_V2, with_module: bool = True,
               with_cf1: bool = True) -> tuple:
        base = Path(d)
        rel = Path("Documents") / "МсрТЧ43" / "Forms" / "ФормаДокумента"
        src, dump1, dump2 = base / "src", base / "d1", base / "d2"
        for root in (src, dump1, dump2):
            (root / rel / "Ext" / "Form").mkdir(parents=True, exist_ok=True)
        # оболочка от cfe-borrow: Adopted + ExtendedConfigurationObject=uuid cf1
        (src / rel.parent / "ФормаДокумента.xml").write_text(
            self.WRAPPER % (self.U_EXT, self.U_CF1), encoding="utf-8-sig")
        (src / rel / "Ext" / "Form.xml").write_text(self.CONTENT_V1, encoding="utf-8-sig")
        if with_cf1:
            # MetaDataObject формы в cf1: uuid формы = U_CF1
            (dump1 / rel.parent / "ФормаДокумента.xml").write_text(
                self.WRAPPER % (self.U_CF1, self.U_CF1), encoding="utf-8-sig")
        (dump2 / rel / "Ext" / "Form.xml").write_text(content, encoding="utf-8-sig")
        if with_module:
            (dump2 / rel / "Ext" / "Form" / "Module.bsl").write_text(
                self.MODULE, encoding="utf-8-sig", newline="")  # как платформа: без трансляции EOL
        return src, dump1, dump2

    def test_form_transfer(self) -> None:
        import tempfile
        from build_ext import fill_form
        with tempfile.TemporaryDirectory() as d:
            src, dump1, dump2 = self._stand(Path(d))
            r = fill_form(src, dump1, dump2, "Documents", "МсрТЧ43", "ФормаДокумента")
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["cf1_uuid"], self.U_CF1)
            self.assertEqual(r["ext_uuid"], self.U_EXT)
            self.assertTrue(r["changed"])
            rel = Path("Documents") / "МсрТЧ43" / "Forms" / "ФормаДокумента"
            self.assertEqual((src / rel / "Ext" / "Form.xml").read_text(encoding="utf-8-sig"),
                             self.CONTENT_V2)  # полный контент cf2, без урезаний
            self.assertEqual((src / rel / "Ext" / "Form" / "Module.bsl").read_text(
                encoding="utf-8-sig").replace("\r\n", "\n"),
                self.MODULE.replace("\r\n", "\n"))

    def test_idempotent_second_call(self) -> None:
        import tempfile
        from build_ext import fill_form
        with tempfile.TemporaryDirectory() as d:
            src, dump1, dump2 = self._stand(Path(d))
            fill_form(src, dump1, dump2, "Documents", "МсрТЧ43", "ФормаДокумента")
            rel = Path("Documents") / "МсрТЧ43" / "Forms" / "ФормаДокумента"
            before = (src / rel / "Ext" / "Form.xml").read_text(encoding="utf-8-sig")
            r = fill_form(src, dump1, dump2, "Documents", "МсрТЧ43", "ФормаДокумента")
            self.assertTrue(r["ok"])
            self.assertFalse(r["changed"])  # контент совпадает — перезаписи нет
            self.assertEqual((src / rel / "Ext" / "Form.xml").read_text(encoding="utf-8-sig"),
                             before)

    def test_module_removed_when_absent_in_cf2(self) -> None:
        import tempfile
        from build_ext import fill_form
        with tempfile.TemporaryDirectory() as d:
            src, dump1, dump2 = self._stand(Path(d))
            fill_form(src, dump1, dump2, "Documents", "МсрТЧ43", "ФормаДокумента")
            rel = Path("Documents") / "МсрТЧ43" / "Forms" / "ФормаДокумента"
            (dump2 / rel / "Ext" / "Form" / "Module.bsl").unlink()
            r = fill_form(src, dump1, dump2, "Documents", "МсрТЧ43", "ФормаДокумента")
            self.assertTrue(r["ok"])
            self.assertTrue(r["changed"])
            self.assertFalse((src / rel / "Ext" / "Form" / "Module.bsl").exists())

    def test_no_cf1_original_rejected(self) -> None:
        import tempfile
        from build_ext import fill_form
        with tempfile.TemporaryDirectory() as d:
            src, dump1, dump2 = self._stand(Path(d), with_cf1=False)
            r = fill_form(src, dump1, dump2, "Documents", "МсрТЧ43", "ФормаДокумента")
            self.assertFalse(r["ok"])  # П15-4: новая форма — не заимствование
            self.assertIn("ExtendedConfigurationObject", r["err"])

    def test_broken_xml_rejected(self) -> None:
        import tempfile
        from build_ext import fill_form
        with tempfile.TemporaryDirectory() as d:
            src, dump1, dump2 = self._stand(Path(d))
            rel = Path("Documents") / "МсрТЧ43" / "Forms" / "ФормаДокумента"
            (dump2 / rel / "Ext" / "Form.xml").write_text("<Form><ChildItems>",
                                                          encoding="utf-8-sig")
            r = fill_form(src, dump1, dump2, "Documents", "МсрТЧ43", "ФормаДокумента")
            self.assertFalse(r["ok"])  # П15-4: отказ ДО сборки, без traceback
            self.assertIn("битый Form.xml", r["err"])

    def test_wrapper_not_adopted_rejected(self) -> None:
        import tempfile
        from build_ext import fill_form
        with tempfile.TemporaryDirectory() as d:
            src, dump1, dump2 = self._stand(Path(d))
            rel = Path("Documents") / "МсрТЧ43" / "Forms"
            bad = self.WRAPPER % (self.U_EXT, self.U_CF1)
            (src / rel / "ФормаДокумента.xml").write_text(
                bad.replace("<ObjectBelonging>Adopted</ObjectBelonging>", ""),
                encoding="utf-8-sig")
            r = fill_form(src, dump1, dump2, "Documents", "МсрТЧ43", "ФормаДокумента")
            self.assertFalse(r["ok"])

    def test_norm_form_xml_canon(self) -> None:
        from build_ext import _norm_form_xml
        a = ('<Form xmlns="http://v8.1c.ru/8.3/xcf/logform"><AutoTime>CurrentOrLast</AutoTime>'
             '<AutoCommandBar name="" id="-1"><ChildItems/></AutoCommandBar>'
             '<Events><Event name="OnCreateAtServer" callType="Before">X</Event></Events></Form>')
        b = ('<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">'
             '<AutoCommandBar name="" id="-1"><ChildItems/></AutoCommandBar>'
             '<Events><Event name="OnCreateAtServer">X</Event></Events></Form>')
        self.assertEqual(_norm_form_xml(a), _norm_form_xml(b))  # канонизация платформы снята
        c = b.replace("<ChildItems/>", "<ChildItems><Button/></ChildItems>")
        self.assertNotEqual(_norm_form_xml(b), _norm_form_xml(c))  # содержательное — ловится

    def test_norm_form_xml_vendor_type_refs(self) -> None:
        """П15-5: платформа в выгрузке расширения разворачивает ссылки на вендорские
        типы в uuid (Type/TypeSet -> TypeId, Item — uuid). Сравнение структурное."""
        from build_ext import _norm_form_xml
        v8 = 'xmlns:v8="http://v8.1c.ru/8.1/data/core"'
        cf2 = ('<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" %s>'
               '<Attributes><Attribute name="Список" id="1"><Settings>'
               '<Filter><ViewSettings><UserSettingsID/>'
               '<Type>cfg:CatalogRef.Номенклатура</Type>'
               '<TypeSet>cfg:DefinedType.Цена</TypeSet>'
               '<Item>FunctionalOption.ИспользоватьХарактеристикиНоменклатуры</Item>'
               '</ViewSettings></Filter></Settings></Attribute></Attributes></Form>' % v8)
        ext = ('<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" %s>'
               '<Attributes><Attribute name="Список" id="1"><Settings>'
               '<Filter><ViewSettings><UserSettingsID/>'
               '<TypeId>190a7469-3325-4d33-b5ec-28a63ac83b06</TypeId>'
               '<TypeId>eefd8557-e29c-43fd-87f1-9c69afa7aa83</TypeId>'
               '<Item>6c8550bb-f6df-4d47-bbea-cfbc7c362006</Item>'
               '</ViewSettings></Filter></Settings></Attribute></Attributes></Form>' % v8)
        self.assertEqual(_norm_form_xml(cf2), _norm_form_xml(ext))
        other = ext.replace("name=\"Список\"", "name=\"Другой\"")
        self.assertNotEqual(_norm_form_xml(ext), _norm_form_xml(other))

    def test_forms_re_paths(self) -> None:
        from build_ext import FORMS_RE
        self.assertIsNotNone(FORMS_RE.match(
            "Documents/МсрТЧ43/Forms/ФормаДокумента/Ext/Form.xml"))
        self.assertIsNotNone(FORMS_RE.match(
            "Documents/МсрТЧ43/Forms/ФормаДокумента/Ext/Form/Module.bsl"))
        self.assertIsNone(FORMS_RE.match("Documents/МсрТЧ43/Forms/ФормаДокумента.xml"))
        self.assertIsNone(FORMS_RE.match("CommonForms/МсрФормаСервер/Ext/Form.xml"))
        self.assertIsNone(FORMS_RE.match("Documents/МсрТЧ43/Ext/ObjectModule.bsl"))


class TestJournalConfigure(Base):
    """v1.2: journal_configure — конфиг моста mcp_bridge.cfg.json + поля сервера."""

    def test_write_and_defaults(self) -> None:
        import json as _json
        cfg_file = Path(self.cfg.root) / "mcp_bridge.cfg.json"
        res = self.tk.journal_configure(enabled=True, kinds=[0, 2], max_mb=10, keep=2)
        self.assertTrue(cfg_file.is_file())
        data = _json.loads(cfg_file.read_text(encoding="utf-8-sig"))
        self.assertEqual(data["journal"]["kinds"], [0, 2])
        self.assertEqual(data["journal"]["maxFileMB"], 10.0)
        self.assertEqual(data["journal"]["keepFiles"], 2)
        self.assertEqual(res["journal"]["enabled"], True)
        # серверные поля синхронизированы
        self.assertEqual(self.tk.cfg.journal_kinds, [0, 2])
        self.assertEqual(self.tk.cfg.journal_max_mb, 10.0)

    def test_partial_update_keeps_other_keys(self) -> None:
        import json as _json
        self.tk.journal_configure(enabled=False, kinds=[1])
        self.tk.journal_configure(max_mb=5)  # частичное обновление поверх
        data = _json.loads((Path(self.cfg.root) / "mcp_bridge.cfg.json")
                           .read_text(encoding="utf-8-sig"))
        self.assertEqual(data["journal"]["enabled"], False)
        self.assertEqual(data["journal"]["kinds"], [1])
        self.assertEqual(data["journal"]["maxFileMB"], 5.0)

    def test_bad_kinds_rejected(self) -> None:
        from mcp_snegopat.bridge import BridgeError
        with self.assertRaises(BridgeError):
            self.tk.journal_configure(kinds=[9])
        with self.assertRaises(BridgeError):
            self.tk.journal_configure(kinds=["AddChild"])

    def test_bad_keep_rejected(self) -> None:
        from mcp_snegopat.bridge import BridgeError
        with self.assertRaises(BridgeError):
            self.tk.journal_configure(keep=0)


class TestMdJournal(Base):
    """Этап 18: чтение журнала MetaDataEvent (JSONL в UTF-16 от моста 1.1+)."""

    def _journal(self, d: Path) -> str:
        p = d / "journal.jsonl"
        recs = [
            {"ts": 1, "pid": 111, "src": "human", "kind": 1, "kindName": "PropChanged",
             "request": True, "obj": {"id": "a", "name": "Реквизит1", "cls": "Реквизит"}},
            {"ts": 2, "pid": 111, "src": "agent", "kind": 0, "kindName": "AddChild",
             "request": False, "obj": {"id": "b", "name": "МсрОбъект", "cls": "Справочник"}},
            {"ts": 3, "pid": 111, "src": "human", "kind": 4, "kindName": "Save",
             "request": False, "obj": {"id": "c", "name": "Конфигурация", "cls": "Конфигурация"}},
        ]
        import io
        with io.open(p, "w", encoding="utf-16") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            fh.write("{обрыв}\n")  # хвостовая порча не роняет чтение
        return str(p)

    def test_read_all_and_tail(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = self._journal(Path(d))
            res = self.tk.md_journal(1, path=p)
            self.assertEqual(res["total"], 3)
            self.assertEqual(res["entries"][-1]["kindName"], "Save")
            res2 = self.tk.md_journal(1, path=p, limit=1)
            self.assertEqual(len(res2["entries"]), 1)
            self.assertEqual(res2["entries"][0]["ts"], 3)

    def test_filters(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = self._journal(Path(d))
            agent = self.tk.md_journal(1, path=p, src="agent")
            self.assertEqual(agent["total"], 1)
            self.assertEqual(agent["entries"][0]["obj"]["name"], "МсрОбъект")
            saves = self.tk.md_journal(1, path=p, kind=4)
            self.assertEqual(saves["total"], 1)

    def test_missing_journal(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            res = self.tk.md_journal(1, path=str(Path(d) / "нет.jsonl"))
            self.assertEqual(res["entries"], [])
            self.assertIn("журнала нет", res["note"])


class TestMakeSnegopatSet(unittest.TestCase):
    """v1.2: включение моста в snegopat.set (хирургическая вставка листа)."""

    LEAF = r'\n{2,0,2,' + r'\n{"S","script:addins\\Autosave.js"},' + r'\n{"B",0},0}'

    def _fixture(self) -> str:
        """Реалистичный .set: заводская шапка + верхний лист (формат 3.0.66:
        внутри ряда после индекса и после loader-строки — лит-\\n)."""
        tree = (r'{"#",e603c0f2-92fb-4d47-8f38-a44a381cf235,\n{1,\n{2,'
                r'\n{0,"Addin",\n{"Pattern"},"",0},\n{1,"isGroup",\n{"Pattern"},"",0}\n},'
                r'\n{2,1,0,0,1,1,\n{1,1,'
                + self.LEAF +
                r'\n},-1,0}\n}\n}')
        return ('Snegopat/Settings/MaximizeWindows\t{"B",1}\n'
                'Snegopat/LoadAddins\t' + tree + '\n')

    def test_leaf_added_with_counter(self) -> None:
        import tempfile
        from make_snegopat_set import add_leaf
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "snegopat.set"
            p.write_text(self._fixture(), encoding="utf-8")
            new_text, changed, msg = add_leaf(p.read_text(encoding="utf-8"))
            self.assertTrue(changed, msg)
            p.write_text(new_text, encoding="utf-8")
            t = p.read_text(encoding="utf-8")
            self.assertIn("mcp_bridge.js", t)
            self.assertIn("-1,1}", t)  # счётчик последнего индекса 0 -> 1

    def test_idempotent(self) -> None:
        import tempfile
        from make_snegopat_set import add_leaf
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "snegopat.set"
            p.write_text(self._fixture(), encoding="utf-8")
            new_text, changed, _ = add_leaf(p.read_text(encoding="utf-8"))
            again, changed2, msg2 = add_leaf(new_text)
            self.assertFalse(changed2)
            self.assertIn("уже есть", msg2)
            self.assertEqual(again, new_text)

    def test_missing_key_rejected(self) -> None:
        from make_snegopat_set import add_leaf
        new_text, changed, msg = add_leaf("Другой/Ключ\t{\"B\",1}\n")
        self.assertFalse(changed)
        self.assertIn("нет ключа", msg)

    def test_row_format_matches_neighbors(self) -> None:
        import re, tempfile
        from make_snegopat_set import add_leaf
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "snegopat.set"
            p.write_text(self._fixture(), encoding="utf-8")
            new_text, _, _ = add_leaf(p.read_text(encoding="utf-8"))
            for line in new_text.splitlines():
                if line.startswith("Snegopat/LoadAddins	"):
                    v = line.split("	", 1)[1]
            our = re.search(r'\{2,1,2,\\n\{"S","script:addins\\\\mcp\\\\mcp_bridge\.js"\},'
                            r'\\n\{"B",0\},0\}', v)
            self.assertIsNotNone(our)

class TestDoctor(Base):
    """v1.2.1: doctor — самодиагностика (файловый чек-лист на подставном доме)."""

    def _prepare_home(self, d: Path, marker: bool = True, flag: bool = True,
                      dwx: bool = True, cfg: bool = True) -> Path:
        home = Path(d) / "home"
        (home / "mcp").mkdir(parents=True)
        if marker:
            (home / "mcp" / "mcp_bridge.home").write_text("", encoding="utf-8")
        if flag:
            (home / "mcp" / "mcp_bridge.on").write_text("", encoding="utf-8")
        if cfg:
            import json as _json
            (home / "mcp" / "mcp_snegopat.json").write_text(
                _json.dumps({"root": str(home / "mcp"),
                             "own_bases": ['File="' + str(home) + '"']}),
                encoding="utf-8")
        return home

    def test_verdict_ok_on_fresh_home(self) -> None:
        """Стартер не запущен (нет starter.exe) → starter=fail — проверяем только
        файловые проверки без стартера: run_filesystem_checks(with_starter=False).
        Свежий дом без load-маркера даёт легитимный warn (конфигуратор не
        перезапускался) — вердикт warn, не fail."""
        import tempfile
        from doctor import run_filesystem_checks
        with tempfile.TemporaryDirectory() as d:
            home = self._prepare_home(Path(d))
            res = run_filesystem_checks(home, with_starter=False)
            names = {c["name"]: c["status"] for c in res["checks"]}
            self.assertEqual(names["дом (маркер mcp_bridge.home)"], "ok")
            self.assertEqual(names["флаг включения mcp_bridge.on"], "ok")
            self.assertEqual(names["конфиг сервера"], "ok")
            self.assertEqual(names["own_bases"], "ok")
            self.assertEqual(res["verdict"], "warn")
            self.assertIn("warn", [c["status"] for c in res["checks"]
                                   if c["name"] == "маркер загрузки моста"])

    def test_missing_marker_and_flag_fail(self) -> None:
        import tempfile
        from doctor import run_filesystem_checks
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            res = run_filesystem_checks(home, with_starter=False)
            fails = [c["name"] for c in res["checks"] if c["status"] == "fail"]
            self.assertIn("дом (маркер mcp_bridge.home)", fails)
            self.assertIn("флаг включения mcp_bridge.on", fails)
            self.assertEqual(res["verdict"], "fail")

    def test_dwx_unregistered_is_fail(self) -> None:
        """DWX: если в HKCU ключа нет — fail (на машине разработки он есть;
        подставной дом не влияет на реестр)."""
        import tempfile
        from doctor import check_dwx
        c = check_dwx()
        self.assertIn(c["status"], ("ok", "fail"))  # зависит от машины, но статус валиден

    def test_tool_doctor_with_live_fake_session(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            home = self._prepare_home(Path(d))
            (home / "mcp" / "mcp_bridge-load.txt").write_text("loaded", encoding="utf-8")
            self.cfg.root = home / "mcp"
            res = self.tk.doctor(session=1)
            statuses = {c["name"]: c["status"] for c in res["checks"]}
            self.assertIn("живая сессия", statuses)
            self.assertEqual(statuses["дом (маркер mcp_bridge.home)"], "ok")




class TestCreateOwnObject(unittest.TestCase):
    """Этап 19: новый объект cf2 -> собственный объект расширения."""

    WRAPPER = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
               'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
               '<CommonForm uuid="{uuid}"><Properties>'
               '<Name>{name}</Name><Synonym/><FormType>Managed</FormType>'
               '</Properties></CommonForm></MetaDataObject>')

    def _dump2(self, d: Path) -> Path:
        dump2 = d / "dump2"
        (dump2 / "CommonForms" / "МсрПриёмка" / "Ext").mkdir(parents=True)
        (dump2 / "CommonForms" / "МсрПриёмка.xml").write_text(
            self.WRAPPER.format(uuid="7c63b0cb-1ad3-4aa8-bd9e-6c7ff81c9a06",
                                name="МсрПриёмка"), encoding="utf-8-sig")
        (dump2 / "CommonForms" / "МсрПриёмка" / "Ext" / "Form.xml").write_text(
            '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform">'
            '<Attributes><Attribute name="Список" id="1"><Settings xsi:type="DynamicList">'
            '<MainTable>Document.МсрТЧ43</MainTable></Settings></Attribute></Attributes></Form>',
            encoding="utf-8-sig")
        return dump2

    def test_own_object_created(self) -> None:
        import tempfile
        from build_ext import create_own_object
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            dump2 = self._dump2(d)
            src = d / "src"
            r = create_own_object(src, dump2, "CommonForms", "CommonForm",
                                  "МсрПриёмка", "МсрП_МсрПриёмка")
            self.assertTrue(r["ok"])
            w = (src / "CommonForms" / "МсрП_МсрПриёмка.xml").read_text(encoding="utf-8-sig")
            self.assertIn("<Name>МсрП_МсрПриёмка</Name>", w)      # имя с префиксом
            self.assertNotIn("<Name>МсрПриёмка</Name>", w)        # старое имя заменено
            self.assertNotIn("7c63b0cb", w)              # uuid НОВЫЙ (own = другой объект)
            self.assertNotIn("ObjectBelonging", w)       # own, не Adopted
            f = (src / "CommonForms" / "МсрП_МсрПриёмка" / "Ext" / "Form.xml") \
                .read_text(encoding="utf-8-sig")
            self.assertIn("Document.МсрТЧ43", f)         # чужие ссылки не тронуты

    def test_rename_own_refs(self) -> None:
        from build_ext import _rename_refs
        # квалифицированные ссылки на САМ объект переименовываются
        self.assertEqual(
            _rename_refs("Document.МсрПриёмка cfg:DocumentRef.МсрПриёмка",
                         "Document", "МсрПриёмка", "МсрП_МсрПриёмка"),
            "Document.МсрП_МсрПриёмка cfg:DocumentRef.МсрП_МсрПриёмка")
        # чужие объекты с похожими именами не тронуты
        self.assertEqual(
            _rename_refs("Document.МсрПриёмка2 Document.Приёмка Catalog.МсрПриёмка",
                         "Document", "МсрПриёмка", "МсрП_МсрПриёмка"),
            "Document.МсрПриёмка2 Document.Приёмка Catalog.МсрПриёмка")

    def test_idempotent_uuid(self) -> None:
        import tempfile
        from build_ext import create_own_object
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            dump2 = self._dump2(d)
            src = d / "src"
            create_own_object(src, dump2, "CommonForms", "CommonForm",
                              "МсрПриёмка", "МсрП_МсрПриёмка")
            u1 = (src / "CommonForms" / "МсрП_МсрПриёмка.xml") \
                .read_text(encoding="utf-8-sig")
            create_own_object(src, dump2, "CommonForms", "CommonForm",
                              "МсрПриёмка", "МсрП_МсрПриёмка")
            u2 = (src / "CommonForms" / "МсрП_МсрПриёмка.xml") \
                .read_text(encoding="utf-8-sig")
            self.assertEqual(u1, u2)  # контент тот же — uuid стабилен

    def test_registration_in_configuration(self) -> None:
        import tempfile
        from build_ext import NEW_OWN_KINDS
        self.assertIn("ОбщаяФорма", NEW_OWN_KINDS)
        self.assertEqual(NEW_OWN_KINDS["ОбщаяФорма"], ("CommonForms", "CommonForm"))


class TestRefCheck(Base):
    """Этап 17: поиск ссылок перед удалением (скан XML-выгрузки)."""

    def _dump(self, d: Path) -> Path:
        dump = d / "dump"
        (dump / "Documents" / "МсрТЧ43" / "Forms" / "ФормаДокумента" / "Ext").mkdir(parents=True)
        (dump / "Documents" / "МсрТЧ43.xml").write_text(
            '<MetaDataObject><Document uuid="1"><Properties><Name>МсрТЧ43</Name>'
            '</Properties></Document></MetaDataObject>', encoding="utf-8-sig")
        # собственная форма: ссылка на хозяина — должна быть исключена по пути
        (dump / "Documents" / "МсрТЧ43" / "Forms" / "ФормаДокумента" / "Ext" / "Form.xml").write_text(
            '<Form><Attributes><Attribute name="Объект" id="1"><Type>'
            '<v8:Type>cfg:DocumentObject.МсрТЧ43</v8:Type></Type></Attribute>'
            '</Attributes></Form>', encoding="utf-8-sig")
        # чужой объект со ссылкой — должен быть найден
        (dump / "Documents" / "Другой.xml").write_text(
            '<MetaDataObject><Document uuid="2"><Properties><Name>Другой</Name></Properties>'
            '<ChildObjects><Attribute uuid="9"><Properties><Name>Рек</Name>'
            '<Type><v8:Type>cfg:DocumentRef.МсрТЧ43</v8:Type></Type></Properties>'
            '</Attribute></ChildObjects></Document></MetaDataObject>', encoding="utf-8-sig")
        # чужая форма со ссылкой через MainTable
        (dump / "Documents" / "Другой" / "Forms" / "ФормаСписка" / "Ext").mkdir(parents=True)
        (dump / "Documents" / "Другой" / "Forms" / "ФормаСписка" / "Ext" / "Form.xml").write_text(
            '<Form><Attributes><Attribute name="Список" id="1"><Settings xsi:type="DynamicList">'
            '<MainTable>Document.МсрТЧ43</MainTable></Settings></Attribute></Attributes></Form>',
            encoding="utf-8-sig")
        return dump

    def test_refs_found_outside_own_subtree(self) -> None:
        import tempfile
        from ref_check import scan_dump
        with tempfile.TemporaryDirectory() as d:
            res = scan_dump(self._dump(Path(d)), "Документ.МсрТЧ43")
            self.assertTrue(res["ok"])
            self.assertTrue(res["refs_found"])
            files = {h["file"] for h in res["hits"]}
            self.assertIn("Documents/Другой.xml", files)
            self.assertIn("Documents/Другой/Forms/ФормаСписка/Ext/Form.xml", files)
            # собственное поддерево исключено
            self.assertFalse(any(f.startswith("Documents/МсрТЧ43") for f in files))

    def test_no_refs(self) -> None:
        import tempfile
        from ref_check import scan_dump
        with tempfile.TemporaryDirectory() as d:
            dump = self._dump(Path(d))
            (dump / "Documents" / "Другой.xml").unlink()
            (dump / "Documents" / "Другой" / "Forms" / "ФормаСписка" / "Ext" / "Form.xml").unlink()
            res = scan_dump(dump, "Документ.МсрТЧ43")
            self.assertTrue(res["ok"])
            self.assertFalse(res["refs_found"])

    def test_unknown_kind_rejected(self) -> None:
        import tempfile
        from ref_check import scan_dump
        with tempfile.TemporaryDirectory() as d:
            res = scan_dump(Path(d), "ЧтоТоСтранное.Имя")
            self.assertFalse(res["ok"])

    def test_md_check_refs_via_dump_dir(self) -> None:
        import tempfile
        from mcp_snegopat.bridge import BridgeError
        with tempfile.TemporaryDirectory() as d:
            dump = self._dump(Path(d))
            res = self.tk.md_check_refs("Документ.МсрТЧ43", dump_dir=str(dump))
            self.assertTrue(res["refs_found"])
            self.assertEqual(res["dump"], str(dump))
            with self.assertRaises(BridgeError):
                self.tk.md_check_refs("СтранныйВид.Имя", dump_dir=str(dump))


class TestExtBuildFromCf(Base):
    """Этап 16: MCP-инструмент ext_build_from_cf — валидация аргументов."""

    def test_missing_cf_rejected(self) -> None:
        from mcp_snegopat.bridge import BridgeError
        with self.assertRaises(BridgeError) as cm:
            self.tk.ext_build_from_cf(str(self.root / "нет.cf"), str(self.root / "нет2.cf"),
                                      "МсрД16", "Мср16_")
        self.assertIn("cf не найден", str(cm.exception))

    def test_empty_name_rejected(self) -> None:
        from mcp_snegopat.bridge import BridgeError
        with self.assertRaises(BridgeError) as cm:
            self.tk.ext_build_from_cf("x.cf", "y.cf", "", "")
        self.assertIn("name и prefix", str(cm.exception))

    def test_work_lock_rejects_parallel(self) -> None:
        """Ревью M9: существующий lock — отказ, чужая сборка не портится."""
        import tempfile
        from mcp_snegopat.bridge import BridgeError
        with tempfile.TemporaryDirectory() as d:
            cf1, cf2 = Path(d) / "a.cf", Path(d) / "b.cf"
            cf1.write_bytes(b"0" * 2000)
            cf2.write_bytes(b"0" * 2000)
            w = Path(d) / "w"
            w.mkdir()
            (w / ".build-ext.lock").write_text("999999")
            with self.assertRaises(BridgeError) as cm:
                self.tk.ext_build_from_cf(str(cf1), str(cf2), "МсрД16", "Мср16_",
                                          work=str(w), timeout_s=5)
            self.assertIn("занят", str(cm.exception))


class TestCollectFormTargets(unittest.TestCase):
    """Ревью C1: детект форм видит УДАЛЁННЫЕ файлы форм (only1)."""

    def test_module_deleted_in_only1_detected(self) -> None:
        from build_ext import collect_form_targets
        rels, deleted = collect_form_targets(
            changed=[], only2=[],
            only1=["Documents/МсрТЧ43/Forms/ФормаДокумента/Ext/Form/Module.bsl"])
        self.assertEqual(rels, {("Documents", "МсрТЧ43", "ФормаДокумента"): True})
        self.assertEqual(deleted,
                         ["Documents/МсрТЧ43/Forms/ФормаДокумента/Ext/Form/Module.bsl"])

    def test_changed_and_new_forms(self) -> None:
        from build_ext import collect_form_targets
        rels, deleted = collect_form_targets(
            changed=["Documents/X/Forms/F1/Ext/Form.xml"],
            only2=["Catalogs/Y/Forms/F2/Ext/Form/Module.bsl"], only1=[])
        self.assertEqual(rels, {("Documents", "X", "F1"): True,
                                ("Catalogs", "Y", "F2"): True})
        self.assertEqual(deleted, [])

    def test_non_form_paths_ignored(self) -> None:
        from build_ext import collect_form_targets
        rels, deleted = collect_form_targets(
            changed=["Documents/X/Ext/ObjectModule.bsl",
                     "CommonForms/МсрФормаСервер/Ext/Form.xml"],
            only2=["Documents/X/Forms/ФормаДокумента.xml"], only1=[])
        self.assertEqual(rels, {})
        self.assertEqual(deleted, [])


class TestDesignerVerdict(unittest.TestCase):
    """Ревью M6: отсутствующий/пустой /DumpResult — отказ, не успех."""

    def test_missing_res_rejected(self) -> None:
        from mcp_to_line import designer_verdict
        ok, info = designer_verdict(0, "", "")
        self.assertFalse(ok)
        self.assertIn("отсутствует", info)

    def test_res_nonzero_rejected(self) -> None:
        from mcp_to_line import designer_verdict
        self.assertFalse(designer_verdict(0, "1", "")[0])

    def test_ok(self) -> None:
        from mcp_to_line import designer_verdict
        ok, info = designer_verdict(0, "0", "Синтаксических ошибок не обнаружено!")
        self.assertTrue(ok)

    def test_fatal_phrase_rejected(self) -> None:
        from mcp_to_line import designer_verdict
        ok, info = designer_verdict(0, "0", "Ошибка при выгрузке")
        self.assertFalse(ok)
        self.assertIn("фатальная", info)


class TestMcpToLineCfe(unittest.TestCase):
    """Этап 16: режим 2 mcp_to_line (--cfe/--ext-name) — валидация."""

    ARGS = dict(line_base="L", dir="D", timeout=10.0)

    def _ns(self, **kw):
        import argparse
        base = dict(self.ARGS)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_cfe_requires_ext_name(self) -> None:
        from mcp_to_line import check_args
        with self.assertRaises(SystemExit):
            check_args(self._ns(cfe="x.cfe", ext_name="", live_base="", objects=""))

    def test_cfe_and_live_mutually_exclusive(self) -> None:
        from mcp_to_line import check_args
        with self.assertRaises(SystemExit):
            check_args(self._ns(cfe="x.cfe", ext_name="М", live_base="B", objects=""))

    def test_cfe_missing_file(self) -> None:
        import tempfile
        from mcp_to_line import check_args
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                check_args(self._ns(cfe=str(Path(d) / "нет.cfe"), ext_name="М",
                                    live_base="", objects=""))

    def test_objects_rejected_in_cfe_mode(self) -> None:
        import tempfile
        from mcp_to_line import check_args
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.cfe"
            f.write_bytes(b"0" * 200)
            with self.assertRaises(SystemExit):
                check_args(self._ns(cfe=str(f), ext_name="М", live_base="",
                                    objects="Справочник.Х"))

    def test_cfe_mode_valid(self) -> None:
        import tempfile
        from mcp_to_line import check_args
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.cfe"
            f.write_bytes(b"0" * 200)
            check_args(self._ns(cfe=str(f), ext_name="М", live_base="", objects=""))

    def test_live_mode_requires_objects(self) -> None:
        from mcp_to_line import check_args
        with self.assertRaises(SystemExit):
            check_args(self._ns(cfe="", ext_name="", live_base="B", objects=""))

    def test_vendor_mismatch_diagnosis(self) -> None:
        """Пилот 04.11.2026: rc=101 «Не найден объект» — вендор приёмника чужой."""
        from mcp_to_line import diagnose_delivery_fail
        log = ("МсрДифБой: Не найден объект ОбщаяКартинка.Набор\r\n"
               "МсрДифБой: Не найден объект Документ.АктВыполненныхРабот")
        msg = diagnose_delivery_fail(log)
        self.assertIn("вендор базы линии не совпадает", msg)
        self.assertIn("Документ.АктВыполненныхРабот", msg)
        self.assertEqual(diagnose_delivery_fail("обычная ошибка"), "")


class TestServerBuild(unittest.TestCase):
    def test_all_tools_registered(self) -> None:
        import asyncio
        with tempfile.TemporaryDirectory() as d:
            mcp = build_server(Config(root=Path(d)))
            names = {t.name for t in asyncio.run(mcp.list_tools())}
        must = {"list_sessions", "md_tree", "md_object", "md_find", "module_get",
                "module_set", "module_patch", "md_set_property", "md_add_child",
                "md_delete", "config_save", "syntax_check", "update_db_cfg",
                "cfgstore_capture", "cfgstore_commit"}
        self.assertTrue(must <= names, f"нет инструментов: {must - names}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
