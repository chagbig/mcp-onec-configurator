# -*- coding: utf-8 -*-
"""uia_dialog_read — гварды платформы и структура CLI (v1.14).

РЕАЛЬНЫЙ UIA-СКАН В ТЕСТАХ НЕ ЗАПУСКАЕТСЯ: проверяются только
- Windows-only гвард (patch sys.platform -> RuntimeError с понятным текстом);
- путь «нет видимых окон» (EnumWindows по несуществующему pid — это не UIA);
- структура CLI (--pid/--title/--out, rc=2 на ошибках окружения,
  SystemExit(2) без обязательного --pid, --help перечисляет ключи);
- константы контракта (MAX_DEPTH=12, MAX_CONTROLS=600, FIELD_TYPES).
Живая проверка экспонирования диалогов 1С — на стенде владельца (Фаза 0).

Прогон: python -m unittest discover -s tests  (из server/, только discover).
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import uia_dialog_read  # noqa: E402
from uia_dialog_read import (  # noqa: E402
    BUTTON_TYPE,
    FIELD_TYPES,
    MAX_CONTROLS,
    MAX_DEPTH,
    main,
    read_dialog,
    require_windows,
)

IS_WINDOWS = sys.platform == "win32"


class TestPlatformGuard(unittest.TestCase):
    """Не-Windows: read_dialog/main -> RuntimeError с понятным текстом."""

    def _read_on(self, platform: str) -> RuntimeError:
        with mock.patch("sys.platform", platform):
            with self.assertRaises(RuntimeError) as cm:
                read_dialog(12345)
        return cm.exception

    def test_read_dialog_raises_on_linux(self) -> None:
        exc = self._read_on("linux")
        self.assertIn("Windows", str(exc))
        self.assertIn("linux", str(exc))

    def test_read_dialog_raises_on_darwin(self) -> None:
        self.assertIn("Windows", str(self._read_on("darwin")))

    def test_require_windows_passes_on_windows(self) -> None:
        if IS_WINDOWS:
            self.assertIsNone(require_windows())  # на живой Windows гвард молчит
        else:
            with self.assertRaises(RuntimeError):
                require_windows()

    def test_cli_rc2_on_linux_no_out_file(self) -> None:
        """CLI на не-Windows: rc=2, --out не создаётся, текст в stderr."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dialog.json"
            with mock.patch("sys.platform", "linux"):
                rc = main(["--pid", "1", "--out", str(out)])
            self.assertEqual(rc, 2)
            self.assertFalse(out.exists())

    def test_lazy_imports_guarded(self) -> None:
        """На не-Windows гвард срабатывает ДО импорта uiautomation/win32."""
        imported_before = set(sys.modules)
        with mock.patch("sys.platform", "linux"):
            with self.assertRaises(RuntimeError):
                read_dialog(1)
        self.assertNotIn("uiautomation", set(sys.modules) - imported_before)


class TestCliStructure(unittest.TestCase):
    """Структура CLI: ключи, обязательность, коды выхода."""

    def test_help_lists_keys(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        help_text = buf.getvalue()
        for key in ("--pid", "--title", "--out"):
            self.assertIn(key, help_text)

    def test_missing_pid_system_exit_2(self) -> None:
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                main([])
        self.assertEqual(cm.exception.code, 2)

    def test_contract_constants(self) -> None:
        self.assertEqual(MAX_DEPTH, 12)
        self.assertEqual(MAX_CONTROLS, 600)
        self.assertEqual(
            FIELD_TYPES,
            frozenset({"EditControl", "ComboBoxControl", "DocumentControl",
                       "CheckBoxControl", "TabItemControl", "TreeControl"}))
        self.assertEqual(BUTTON_TYPE, "ButtonControl")
        self.assertTrue(callable(read_dialog))
        self.assertTrue(callable(main))


@unittest.skipUnless(IS_WINDOWS, "гвард платформы: тест на Windows")
class TestWindowsNoWindows(unittest.TestCase):
    """Windows-путь без UIA: EnumWindows по pid без окон -> RuntimeError."""

    def test_read_dialog_no_windows_for_empty_pid(self) -> None:
        """pid 0 (System Idle) не владеет окнами: EnumWindows пуст, UIA не трогается."""
        with self.assertRaises(RuntimeError) as cm:
            read_dialog(0)
        self.assertIn("нет видимых окон", str(cm.exception))

    def test_cli_no_windows_rc2(self) -> None:
        rc = main(["--pid", "0"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
