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
