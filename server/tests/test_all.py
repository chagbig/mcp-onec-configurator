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

from fake_bridge import FakeBridge  # noqa: E402
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
