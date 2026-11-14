# -*- coding: utf-8 -*-
"""№10: управление конфигурацией моста/сервера — Config.op_timeouts/long_ops,
резолюция таймаута в Bridge.call (fake-мост) и tools/bridge_configure.py."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from fake_bridge import FakeBridge  # noqa: E402
from mcp_snegopat.bridge import Bridge, BridgeError, LONG_OPS, resolve_timeout  # noqa: E402
from mcp_snegopat.config import Config, load_config  # noqa: E402
from bridge_configure import configure  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BRIDGE_JS = ROOT / "bridge" / "mcp_bridge.js"


def _read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


class TestResolveTimeout(unittest.TestCase):
    """Чистая функция резолюции: per-op -> long -> default."""

    def test_config_fields_defaults(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.op_timeouts, {})
        self.assertEqual(cfg.long_ops, [])

    def test_plain_op_uses_default(self) -> None:
        self.assertEqual(resolve_timeout(Config(), "md_tree"),
                         Config().default_timeout_s)

    def test_builtin_long_ops(self) -> None:
        cfg = Config()
        for op in LONG_OPS:
            self.assertEqual(resolve_timeout(cfg, op), cfg.long_timeout_s)

    def test_custom_long_ops_extends_not_replaces(self) -> None:
        cfg = Config(long_ops=["form_get_xml", "config_open"])  # config_open уже встроенный
        self.assertEqual(resolve_timeout(cfg, "form_get_xml"), 900.0)
        self.assertEqual(resolve_timeout(cfg, "config_open"), 900.0)
        self.assertEqual(resolve_timeout(cfg, "md_tree"), 120.0)

    def test_per_op_override_beats_long(self) -> None:
        cfg = Config(op_timeouts={"config_open": 60.0})
        self.assertEqual(resolve_timeout(cfg, "config_open"), 60.0)

    def test_per_op_override_on_plain_op(self) -> None:
        cfg = Config(op_timeouts={"ping": 5})
        self.assertEqual(resolve_timeout(cfg, "ping"), 5.0)

    def test_bad_per_op_value_ignored(self) -> None:
        cfg = Config(op_timeouts={"ping": 0, "md_tree": -3.0})
        self.assertEqual(resolve_timeout(cfg, "ping"), 120.0)
        self.assertEqual(resolve_timeout(cfg, "md_tree"), 120.0)

    def test_load_config_accepts_new_keys(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp-snegopat-cfg-") as d:
            p = Path(d) / "mcp_snegopat.json"
            p.write_text(json.dumps({"op_timeouts": {"config_open": 60},
                                     "long_ops": ["md_check_refs"]}),
                         encoding="utf-8")
            cfg = load_config(p)
        self.assertEqual(cfg.op_timeouts, {"config_open": 60.0})
        self.assertEqual(cfg.long_ops, ["md_check_refs"])

    def test_load_config_rejects_bad_op_timeouts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp-snegopat-cfg-") as d:
            p = Path(d) / "mcp_snegopat.json"
            p.write_text(json.dumps({"op_timeouts": {"ping": -1}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(p)


class TestBridgeCallTimeouts(unittest.TestCase):
    """Резолюция применяется в Bridge.call на fake-мосте: slow_op спит 1.2 с,
    per-op override 0.5 с роняет вызов по таймауту КЛИЕНТА (а не 120 с умолчания)."""

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-snegopat-tmo-")
        self.root = Path(self.tmp.name)
        self.fake = FakeBridge(self.root, pid=os.getpid()).start()
        for _ in range(300):
            if self.fake_live():
                break
            time.sleep(0.01)
        else:
            self.fail("подставной мост не поднялся за 3 с")

    def fake_live(self) -> bool:
        return Bridge(Config(root=self.root)).live_sessions() != []

    def tearDown(self) -> None:
        self.fake.stop()
        self.tmp.cleanup()

    def _call(self, cfg: Config, op: str) -> dict:
        cfg.journal_path = None  # журнал тестов не пишем
        return Bridge(cfg).call(self.fake.pid, op)

    def test_slow_op_with_per_op_override_times_out(self) -> None:
        cfg = Config(root=self.root, op_timeouts={"slow_op": 0.5})
        t0 = time.time()
        with self.assertRaises(BridgeError) as cm:
            self._call(cfg, "slow_op")
        elapsed = time.time() - t0
        self.assertEqual(cm.exception.code, "timeout")
        self.assertLess(elapsed, 10.0, "пер-op таймаут не применился (ждёт 120 с?)")
        self.assertGreaterEqual(elapsed, 0.4)

    def test_slow_op_with_defaults_completes(self) -> None:
        cfg = Config(root=self.root)
        res = self._call(cfg, "slow_op")
        self.assertEqual(res.get("slept"), 1.2)


class TestBridgeConfigure(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-snegopat-bconf-")
        self.d = Path(self.tmp.name)
        self.server_cfg = self.d / "mcp_snegopat.json"
        self.bridge_cfg = self.d / "mcp" / "mcp_bridge.cfg.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_server_cfg(self, data: dict) -> None:
        self.server_cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_updates_fields_preserving_existing_keys(self) -> None:
        self._write_server_cfg({"root": str(self.d / "mcp"), "allow_store": True,
                                "custom_key": 42})
        res = configure(self.server_cfg, default_timeout_s=60,
                        op_timeouts={"config_open": 45},
                        long_ops=["md_check_refs"])
        data = _read(self.server_cfg)
        self.assertEqual(data["custom_key"], 42)          # чужие ключи целы
        self.assertEqual(data["root"], str(self.d / "mcp"))
        self.assertEqual(data["allow_store"], True)
        self.assertEqual(data["default_timeout_s"], 60.0)
        self.assertEqual(data["op_timeouts"], {"config_open": 45.0})
        self.assertEqual(data["long_ops"], ["md_check_refs"])
        self.assertEqual(set(res["applied"]),
                         {"default_timeout_s", "op_timeouts", "long_ops"})

    def test_no_args_changes_nothing(self) -> None:
        self._write_server_cfg({"root": str(self.d / "mcp")})
        before = self.server_cfg.read_text(encoding="utf-8-sig")
        res = configure(self.server_cfg)
        self.assertEqual(res["applied"], {})
        self.assertEqual(self.server_cfg.read_text(encoding="utf-8-sig"), before)

    def test_whitelist_preserves_journal_section(self) -> None:
        self.bridge_cfg.parent.mkdir(parents=True)
        self.bridge_cfg.write_text(json.dumps(
            {"journal": {"enabled": False, "kinds": [1, 2], "maxFileMB": 5.0}},
            ensure_ascii=False), encoding="utf-8")
        res = configure(self.server_cfg, bridge_cfg_path=self.bridge_cfg,
                        cfg_cmd_whitelist=[35, 38, 41])
        data = _read(self.bridge_cfg)
        self.assertEqual(data["cfg_cmd_whitelist"], [35, 38, 41])
        self.assertEqual(data["journal"]["enabled"], False)   # journal не тронут
        self.assertEqual(data["journal"]["kinds"], [1, 2])
        self.assertEqual(data["journal"]["maxFileMB"], 5.0)
        self.assertEqual(res["bridge_cfg"], str(self.bridge_cfg))

    def test_whitelist_creates_missing_file(self) -> None:
        res = configure(self.server_cfg, bridge_cfg_path=self.bridge_cfg,
                        cfg_cmd_whitelist=[10])
        data = _read(self.bridge_cfg)
        self.assertEqual(data, {"cfg_cmd_whitelist": [10]})
        self.assertEqual(res["bridge_cfg"], str(self.bridge_cfg))

    def test_whitelist_path_derived_from_root(self) -> None:
        self._write_server_cfg({"root": str(self.d / "mcp")})
        configure(self.server_cfg, cfg_cmd_whitelist=[35])
        self.assertTrue((self.d / "mcp" / "mcp_bridge.cfg.json").is_file())
        self.assertEqual(_read(self.d / "mcp" / "mcp_bridge.cfg.json"),
                         {"cfg_cmd_whitelist": [35]})

    def test_whitelist_without_root_fails(self) -> None:
        self._write_server_cfg({"allow_store": True})
        with self.assertRaises(ValueError) as cm:
            configure(self.server_cfg, cfg_cmd_whitelist=[35])
        self.assertIn("root", str(cm.exception))

    def test_validation_errors(self) -> None:
        self._write_server_cfg({})
        for kwargs in ({"default_timeout_s": 0}, {"long_timeout_s": -1},
                       {"heartbeat_stale_s": "abc"},
                       {"op_timeouts": {"x": -1}}, {"op_timeouts": [1]},
                       {"long_ops": [3]}, {"long_ops": [""]},
                       {"cfg_cmd_whitelist": ["35"]}, {"cfg_cmd_whitelist": [0]},
                       {"cfg_cmd_whitelist": [True]}):
            with self.assertRaises(ValueError):
                configure(self.server_cfg, **kwargs)
        self.assertEqual(_read(self.server_cfg), {})  # файл не записан мусором

    def test_note_mentions_restart(self) -> None:
        self._write_server_cfg({"root": str(self.d / "mcp")})
        res = configure(self.server_cfg, default_timeout_s=60,
                        cfg_cmd_whitelist=[35])
        self.assertIn("перезапуск", res["note"])
        self.assertIn("cfg_cmd_whitelist", res["note"])


class TestJsBalanceBridge(unittest.TestCase):
    """jsbalance на mcp_bridge.js: баланс скобок с учётом строк/комментариев/regex
    (node нет, cscript не принимает кириллические идентификаторы)."""

    def test_bridge_balanced(self) -> None:
        self.assertTrue(BRIDGE_JS.is_file(), f"нет {BRIDGE_JS}")
        r = subprocess.run([sys.executable,
                            str(Path(__file__).resolve().parent / "jsbalance.py"),
                            str(BRIDGE_JS)],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("BALANCED OK", r.stdout)

    def test_bridge_es3_syntax(self) -> None:
        """ES3 (JScript 5): никаких let/const/стрелок — jsbalance их не ловит."""
        src = BRIDGE_JS.read_text(encoding="utf-8-sig")
        bad = [(m.start(), m.group()) for m in
               re.finditer(r"\blet\s|\bconst\s|=>", src)]
        self.assertEqual(bad, [], f"не-ES3 конструкции в mcp_bridge.js: {bad[:5]}")


if __name__ == "__main__":
    unittest.main()
