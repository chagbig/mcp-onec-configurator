r""""№431 (адаптация «аудитор молчаливых фолбэков»): doctor — ДРЕЙФ.

«Дрейф моста»: md5 развёрнутого mcp_bridge.js (все <дом>\snegopat\<вер>\ и
легаси) vs канон <репо>\bridge\mcp_bridge.js — deployed-мост может отстать
от репозитория. «Дрейф конфига»: runtime-конфиг MCP-сервера vs файл
MCP_SNEGOPAT_CONFIG — поля применены runtime'ом до последней правки файла.

Все тесты герметичны: репо-мост и установки подставляются в tmp через
mock.patch("doctor.REPO_BRIDGE", ...) — реальный репозиторий не читается.
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "tools"))

import doctor  # noqa: E402
from doctor import (bridge_deploy_paths, check_bridge_drift, check_config_drift,  # noqa: E402
                    run_filesystem_checks, _md5_prefix, _runtime_cfg)
from mcp_snegopat.config import Config  # noqa: E402


class BridgeDriftBase(unittest.TestCase):
    """Подставной дом + подставной «репозиторий» (замена doctor.REPO_BRIDGE)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-drift-test-")
        self.d = Path(self.tmp.name)
        self.home = self.d / "home"
        self.home.mkdir()
        self.repo_bridge = self.d / "repo" / "bridge" / "mcp_bridge.js"
        self.repo_bridge.parent.mkdir(parents=True)
        self.repo_bridge.write_text("// canonical bridge v1\n", encoding="utf-8")
        # герметичность: реальный репозиторий не участвует
        patcher = mock.patch.object(doctor, "REPO_BRIDGE", self.repo_bridge)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def deploy(self, rel: str, content: str | None = None) -> Path:
        """Создать установку снегопата: rel — путь под home до каталога addins."""
        addins = self.home / rel
        addins.mkdir(parents=True)
        f = addins / "mcp_bridge.js"
        f.write_text("// canonical bridge v1\n" if content is None else content,
                     encoding="utf-8")
        return f


class TestBridgeDrift(BridgeDriftBase):
    # платформонейтральный путь установки (без ручных бэкслешей — на Linux
    # «snegopat\1.0\...» это ОДНО имя каталога, мост не найдётся)
    VER = str(Path("snegopat") / "1.0" / "x64" / "modules" / "snegopat"
              / "addins" / "mcp")

    def test_deployed_matches_repo_is_ok(self) -> None:
        f = self.deploy(self.VER)  # копия канона
        c = check_bridge_drift(self.home)
        self.assertEqual(c["status"], "ok")
        self.assertIn(str(f), c["detail"])
        self.assertEqual(f.read_bytes(), self.repo_bridge.read_bytes())

    def test_deployed_differs_is_warn_with_md5_prefixes(self) -> None:
        self.deploy(self.VER, "// OLD bridge (deploy отстал)\n")
        c = check_bridge_drift(self.home)
        self.assertEqual(c["status"], "warn")
        self.assertIn(self.VER, c["detail"])
        # md5-префиксы (по 8 символов), разные между deploy и repo
        dep = _md5_prefix(self.home / self.VER / "mcp_bridge.js")
        rep = _md5_prefix(self.repo_bridge)
        self.assertEqual(len(dep), 8)
        self.assertEqual(len(rep), 8)
        self.assertNotEqual(dep, rep)
        self.assertIn(dep, c["detail"])
        self.assertIn(rep, c["detail"])
        # полные хеши не пугают: префикс короткий
        self.assertNotIn(dep + _md5_prefix(self.repo_bridge), c["detail"])

    def test_repo_missing_is_note(self) -> None:
        self.deploy(self.VER)
        self.repo_bridge.unlink()
        c = check_bridge_drift(self.home)
        self.assertEqual(c["status"], "note")
        self.assertIn("репозиторий", c["detail"])

    def test_no_deploy_is_note(self) -> None:
        c = check_bridge_drift(self.home)
        self.assertEqual(c["status"], "note")
        self.assertIn("не найден", c["detail"])
        self.assertEqual(bridge_deploy_paths(self.home), [])

    def test_legacy_path_checked(self) -> None:
        f = self.deploy(str(Path("x64") / "modules" / "snegopat" / "addins"
                            / "mcp"))  # легаси v1.1.x
        self.assertEqual(bridge_deploy_paths(self.home), [f])
        self.assertEqual(check_bridge_drift(self.home)["status"], "ok")
        # легаси без подкаталога mcp (adder прямо в addins) тоже виден
        alt = self.home / Path("x64") / "modules" / "snegopat" / "addins"
        alt.mkdir(parents=True, exist_ok=True)
        (alt / "mcp_bridge.js").write_text("// legacy plain\n", encoding="utf-8")
        c = check_bridge_drift(self.home)
        self.assertEqual(c["status"], "warn")
        self.assertIn(str(alt / "mcp_bridge.js"), c["detail"])

    def test_all_versions_checked_one_drift_warns(self) -> None:
        """Несколько версий снегопата: проверяются ВСЕ; warn перечисляет
        именно отставшую установку, совпавшие считаются отдельно."""
        self.deploy(str(Path("snegopat") / "2.0" / "x64" / "modules"
                        / "snegopat" / "addins" / "mcp"))
        old = self.deploy(str(Path("snegopat") / "1.0" / "x64" / "modules"
                              / "snegopat" / "addins" / "mcp"),
                          "// stale 1.0\n")  # noqa: F841 — отставшая установка
        c = check_bridge_drift(self.home)
        self.assertEqual(c["status"], "warn")
        self.assertIn("1.0", c["detail"])
        self.assertIn("deploy", c["detail"])
        self.assertIn("совпадают: 1", c["detail"])
        # совпавшая 2.0 в warn-детале не перечисляется (только счётчик)
        self.assertNotIn("snegopat\\2.0", c["detail"])

    def test_verdict_not_worsened_by_note(self) -> None:
        """note не влияет на вердикт: пустой дом (нет deploy) — note,
        вердикт определяется прежними проверками (fail без маркера/флага)."""
        with mock.patch.dict(sys.modules, {"mcp_snegopat.server": None}):
            res = run_filesystem_checks(self.home, with_starter=False)
        names = {c["name"]: c["status"] for c in res["checks"]}
        self.assertEqual(names["дрейф моста"], "note")
        self.assertEqual(res["verdict"], "fail")  # маркер/флаг отсутствуют

    def test_drift_check_present_in_filesystem_checks(self) -> None:
        self.deploy(self.VER)
        with mock.patch.dict(sys.modules, {"mcp_snegopat.server": None}):
            res = run_filesystem_checks(self.home, with_starter=False)
        names = {c["name"] for c in res["checks"]}
        self.assertIn("дрейф моста", names)

    def test_cli_context_has_no_config_drift(self) -> None:
        """CLI-контекст (mcp_snegopat.server не импортирован): «дрейф конфига»
        не добавляется — сверять не с чем."""
        with mock.patch.dict(sys.modules, {"mcp_snegopat.server": None}), \
                mock.patch.dict("os.environ", {}, clear=False):
            import os as _os
            _os.environ.pop("MCP_SNEGOPAT_CONFIG", None)
            res = run_filesystem_checks(self.home, with_starter=False)
        self.assertNotIn("дрейф конфига", {c["name"] for c in res["checks"]})


class TestConfigDrift(unittest.TestCase):
    """Дрейф конфига: runtime cfg vs файл MCP_SNEGOPAT_CONFIG (env)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-drift-cfg-test-")
        self.d = Path(self.tmp.name)
        self.cfg_file = self.d / "mcp_snegopat.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _env(self, exists: bool = True):
        env = {"MCP_SNEGOPAT_CONFIG": str(self.cfg_file)} if exists else {}
        return mock.patch.dict("os.environ", env, clear=False)

    def _write_cfg(self, data: dict) -> None:
        self.cfg_file.write_text(json.dumps(data), encoding="utf-8")

    def test_same_scalars_is_ok(self) -> None:
        self._write_cfg({"default_timeout_s": 120.0, "long_timeout_s": 900.0,
                         "heartbeat_stale_s": 30.0, "read_only": False,
                         "presets": [], "journal_enabled": True,
                         "journal_kinds": [], "journal_max_mb": 20.0,
                         "journal_keep": 3})
        cfg = Config()
        with self._env():
            c = check_config_drift(cfg)
        self.assertEqual(c["status"], "ok")

    def test_differs_is_warn_named_fields(self) -> None:
        self._write_cfg({"default_timeout_s": 60.0, "read_only": True})
        cfg = Config()  # runtime: default 120, read_only False — правка файла позже
        with self._env():
            c = check_config_drift(cfg)
        self.assertEqual(c["status"], "warn")
        self.assertIn("поля применены runtime'ом до файла", c["detail"])
        self.assertIn("default_timeout_s", c["detail"])
        self.assertIn("read_only", c["detail"])

    def test_absent_file_keys_not_compared(self) -> None:
        """Поле, отсутствующее в файле, дрейфом не считается (runtime = умолчание
        или значение ключа CLI — файла это не касается)."""
        self._write_cfg({"own_bases": ['File="C:\\b";']})
        cfg = Config(default_timeout_s=55.0)  # в файле ключа нет
        with self._env():
            c = check_config_drift(cfg)
        self.assertEqual(c["status"], "ok")

    def test_nested_keys_presence_only(self) -> None:
        """op_timeouts/long_ops вложенно не читаются — только наличие ключа:
        одинаковый факт наличия при разном содержимом = ok."""
        self._write_cfg({"op_timeouts": {"config_save": 7777},
                         "long_ops": ["md_check_refs"]})
        cfg = Config(op_timeouts={"cfg_archive_cf": 1}, long_ops=["form_set_xml"])
        with self._env():
            c = check_config_drift(cfg)
        self.assertEqual(c["status"], "ok")
        # а различие факта наличия/отсутствия — warn
        cfg2 = Config()  # пустые op_timeouts/long_ops, в файле ключи есть
        with self._env():
            c2 = check_config_drift(cfg2)
        self.assertEqual(c2["status"], "warn")
        self.assertIn("op_timeouts", c2["detail"])
        self.assertIn("long_ops", c2["detail"])

    def test_no_env_is_note(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False):
            import os as _os
            _os.environ.pop("MCP_SNEGOPAT_CONFIG", None)
            c = check_config_drift(Config())
        self.assertEqual(c["status"], "note")

    def test_env_file_missing_is_note(self) -> None:
        with self._env():
            c = check_config_drift(Config())
        self.assertEqual(c["status"], "note")
        self.assertIn("не найден", c["detail"])

    def test_bad_json_is_warn(self) -> None:
        self.cfg_file.write_text("{not json", encoding="utf-8")
        with self._env():
            c = check_config_drift(Config())
        self.assertEqual(c["status"], "warn")
        self.assertIn("не читается", c["detail"])


class TestRuntimeCfgDetection(unittest.TestCase):
    """MCP-блок: cfg запущенного сервера находится через gc (server.py не правится:
    его doctor() зовёт run_filesystem_checks из doctor — mcp_snegopat.server уже
    в sys.modules)."""

    def test_detection_by_home_match(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "mcp"
            root.mkdir()
            cfg = Config(root=root, journal_path=root / "journal.jsonl")

            class Toolkit:  # подставка вместо настоящего server.Toolkit
                def __init__(self, cfg):
                    self.cfg = cfg

            stub = types.ModuleType("mcp_snegopat.server")
            stub.Toolkit = Toolkit
            tk = Toolkit(cfg)  # держим ссылку — gc обязан найти
            with mock.patch.dict(sys.modules, {"mcp_snegopat.server": stub}):
                found = _runtime_cfg(root.parent)
                self.assertIs(found, cfg)
                # без совпадения дома — первый найденный
                self.assertIs(_runtime_cfg(Path(d) / "other"), cfg)
            _ = tk

    def test_no_server_module_returns_none(self) -> None:
        with mock.patch.dict(sys.modules, {"mcp_snegopat.server": None}):
            self.assertIsNone(_runtime_cfg(Path(".")))

    def test_explicit_cfg_param_wins(self) -> None:
        """run_filesystem_checks(cfg=...) добавляет «дрейф конфига» в MCP-блоке,
        env не задан -> note (верди́кт не ухудшается)."""
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            (home / "mcp").mkdir(parents=True)
            (home / "mcp" / "mcp_bridge.home").write_text("", encoding="utf-8")
            (home / "mcp" / "mcp_bridge.on").write_text("", encoding="utf-8")
            with mock.patch.dict(sys.modules, {"mcp_snegopat.server": None}), \
                    mock.patch.dict("os.environ", {}, clear=False):
                import os as _os
                _os.environ.pop("MCP_SNEGOPAT_CONFIG", None)
                res = run_filesystem_checks(home, with_starter=False,
                                            cfg=Config(root=home / "mcp"))
        names = {c["name"]: c["status"] for c in res["checks"]}
        self.assertEqual(names["дрейф конфига"], "note")


if __name__ == "__main__":
    unittest.main()
