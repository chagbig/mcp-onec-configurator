# -*- coding: utf-8 -*-
"""user_watch (карта переходов v1.14): станционный регистратор действий.

Чистые части: Recorder.add_event/flush_jsonl (JSONL UTF-8, ensure_ascii=False,
кириллица в fg_title), порядок 100 событий сохранён, БЕЗОПАСНОСТЬ — key-события
не содержат поля text/char/wch (только VK-коды, WM_CHAR не пишется), фильтр
VK (0x00 и 0xFF отбрасываются). Платформенные части: константы LL-хуков,
раскладка структур KBDLLHOOKSTRUCT/MSLLHOOKSTRUCT и парсеры LPARAM —
skipUnless win32; реальный ввод в тестах НЕ вызывается и количество событий
не проверяется. CLI smoke: --secs 0.3 создаёт jsonl (может быть пустым), rc=0.
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import user_watch  # noqa: E402

TOOL = Path(__file__).resolve().parents[1] / "tools" / "user_watch.py"


class TestRecorder(unittest.TestCase):
    def test_add_event_returns_and_appends(self) -> None:
        rec = user_watch.Recorder()
        ret = rec.add_event({"type": "key", "vk": 0x41})
        self.assertIs(ret, rec.events)
        rec.add_event({"type": "mouse"})
        self.assertEqual(len(rec.events), 2)
        self.assertEqual(rec.events[0]["vk"], 0x41)

    def test_flush_jsonl_fields_and_cyrillic(self) -> None:
        rec = user_watch.Recorder()
        rec.add_event(user_watch.make_mouse_event(
            "down", 101, 202, title="Хранилище — Конфигуратор"))
        rec.add_event(user_watch.make_key_event(0x41, "down", title="Конфигуратор"))
        rec.add_event(user_watch.make_window_event(
            12345, "Диалог «Соединение с хранилищем»"))
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "watch.jsonl"
            n = rec.flush_jsonl(str(path))
            self.assertEqual(n, 3)
            self.assertEqual(rec.events, [])  # буфер очищен
            raw = path.read_text(encoding="utf-8")
            # ensure_ascii=False: кириллица в файле как есть, не \uXXXX
            self.assertIn("Хранилище", raw)
            self.assertIn("Соединение с хранилищем", raw)
            lines = [json.loads(line) for line in raw.splitlines()]
        mouse, key, win = lines
        self.assertEqual(mouse["type"], "mouse")
        self.assertEqual(mouse["ev"], "down")
        self.assertEqual(mouse["x"], 101)
        self.assertEqual(mouse["y"], 202)
        self.assertIsNone(mouse["vk"])
        self.assertEqual(mouse["fg_title"], "Хранилище — Конфигуратор")
        self.assertEqual(key["type"], "key")
        self.assertEqual(key["vk"], 0x41)
        self.assertEqual(key["ev"], "down")
        self.assertEqual(win["type"], "window")
        self.assertEqual(win["ev"], "fg_change")
        self.assertEqual(win["hwnd"], 12345)
        for ev in lines:
            self.assertTrue(ev["t"].endswith("+00:00"))  # ISO UTC

    def test_key_events_have_no_text_field(self) -> None:
        # безопасность: только VK — поля text/char/wch не существуют
        rec = user_watch.Recorder()
        for vk in (0x10, 0x41, 0x43, 0x42, 0x20):
            rec.add_event(user_watch.make_key_event(vk, "down", title="Конфигуратор"))
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "keys.jsonl"
            rec.flush_jsonl(str(path))
            for line in path.read_text(encoding="utf-8").splitlines():
                ev = json.loads(line)
                self.assertEqual(ev["type"], "key")
                self.assertNotIn("text", ev)
                self.assertNotIn("char", ev)
                self.assertNotIn("wch", ev)
                self.assertIsInstance(ev["vk"], int)

    def test_100_events_order_preserved(self) -> None:
        rec = user_watch.Recorder()
        for i in range(100):
            rec.add_event({"seq": i, "type": "key", "vk": 0x41 + (i % 26),
                           "ev": "down", "fg_title": "Конфигуратор"})
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "many.jsonl"
            self.assertEqual(rec.flush_jsonl(str(path)), 100)
            seqs = [json.loads(line)["seq"]
                    for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(seqs, list(range(100)))

    def test_flush_empty(self) -> None:
        rec = user_watch.Recorder()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "empty.jsonl"
            self.assertEqual(rec.flush_jsonl(str(path)), 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_summary_counts(self) -> None:
        rec = user_watch.Recorder()
        rec.add_event({"type": "mouse"})
        rec.add_event({"type": "mouse"})
        rec.add_event({"type": "key"})
        rec.add_event({"type": "window"})
        self.assertEqual(rec.summary(),
                         {"events": 4, "mouse": 2, "key": 1, "windows": 1})


class TestSafetyNoSymbols(unittest.TestCase):
    def test_key_event_schema_exact(self) -> None:
        # схема key-события зафиксирована: никаких текстовых полей кроме
        # заголовка окна (метаданные, не содержимое ввода)
        ev = user_watch.make_key_event(0x41, "up", title="Диалог")
        self.assertEqual(set(ev), {"t", "type", "vk", "ev", "fg_title"})
        self.assertEqual(ev["vk"], 0x41)
        self.assertIsInstance(ev["vk"], int)

    def test_mouse_event_vk_is_none(self) -> None:
        ev = user_watch.make_mouse_event("wheel", 5, 6, title="Окно")
        self.assertEqual(set(ev), {"t", "type", "ev", "x", "y", "vk", "fg_title"})
        self.assertIsNone(ev["vk"])

    def test_vk_filter_bounds(self) -> None:
        self.assertFalse(user_watch.key_vk_allowed(0x00))  # vk < 0x01
        self.assertFalse(user_watch.key_vk_allowed(0xFF))
        self.assertTrue(user_watch.key_vk_allowed(0x01))
        self.assertTrue(user_watch.key_vk_allowed(0x41))   # буква (код)
        self.assertTrue(user_watch.key_vk_allowed(0x2E))   # Delete
        self.assertTrue(user_watch.key_vk_allowed(0xFE))


class TestPlatformGuard(unittest.TestCase):
    def test_non_windows_refused(self) -> None:
        # user32=None — обойти memoization уже инициализированного модуля
        with mock.patch.object(user_watch, "user32", None), \
             mock.patch.object(sys, "platform", "linux"):
            for fn in (user_watch.install_mouse_hook,
                       user_watch.install_key_hook,
                       lambda: user_watch.pump_loop(1)):
                with self.assertRaises(RuntimeError) as cm:
                    fn()
                self.assertIn("Windows", str(cm.exception))


@unittest.skipUnless(sys.platform == "win32",
                     "LL-хуки и user32 доступны только на Windows")
class TestWindowsHooks(unittest.TestCase):
    def test_hook_constants(self) -> None:
        self.assertEqual(user_watch.WH_MOUSE_LL, 14)
        self.assertEqual(user_watch.WH_KEYBOARD_LL, 13)
        self.assertEqual(user_watch.HC_ACTION, 0)

    def test_struct_layouts(self) -> None:
        # x86/x64-независимые смещения: vkCode+0, scanCode+4, flags+8;
        # мышь: pt.x+0, pt.y+4, mouseData+8
        self.assertEqual(user_watch.KBDLLHOOKSTRUCT.vkCode.offset, 0)
        self.assertEqual(user_watch.KBDLLHOOKSTRUCT.scanCode.offset, 4)
        self.assertEqual(user_watch.KBDLLHOOKSTRUCT.flags.offset, 8)
        self.assertEqual(user_watch.MSLLHOOKSTRUCT.pt.offset, 0)
        self.assertEqual(user_watch.wintypes.POINT.x.offset, 0)
        self.assertEqual(user_watch.wintypes.POINT.y.offset, 4)
        self.assertEqual(user_watch.MSLLHOOKSTRUCT.mouseData.offset, 8)

    def test_parse_kbdl_msll_roundtrip(self) -> None:
        kb = user_watch.KBDLLHOOKSTRUCT(vkCode=0x41, scanCode=30, flags=0,
                                        time=1, dwExtraInfo=0)
        self.assertEqual(user_watch.parse_kbdl(ctypes.addressof(kb)).vkCode, 0x41)
        ms = user_watch.MSLLHOOKSTRUCT()
        ms.pt.x, ms.pt.y = 123, 456
        parsed = user_watch.parse_msll(ctypes.addressof(ms))
        self.assertEqual((parsed.pt.x, parsed.pt.y), (123, 456))

    def test_pump_loop_zero_immediate(self) -> None:
        # secs=0: пульт не запускается; количество событий НЕ проверяем
        t0 = time.monotonic()
        user_watch.pump_loop(0)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_fg_title_returns_str(self) -> None:
        self.assertIsInstance(user_watch.fg_title(), str)
        self.assertIsInstance(user_watch.fg_hwnd(), int)


@unittest.skipUnless(sys.platform == "win32",
                     "CLI с LL-хуками доступен только на Windows")
class TestCliSmoke(unittest.TestCase):
    def test_cli_creates_jsonl_rc0(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "watch.jsonl"
            r = subprocess.run([sys.executable, str(TOOL), "--pid", "4",
                                "--out", str(out), "--secs", "0.3"],
                               capture_output=True, text=True,
                               encoding="utf-8", timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(out.is_file(), "jsonl не создан")
            # файл может быть пустым; если события есть — парсим как JSON
            for line in out.read_text(encoding="utf-8").splitlines():
                self.assertIsInstance(json.loads(line), dict)
            summary = json.loads(r.stdout.strip().splitlines()[-1])
            self.assertEqual(set(summary), {"events", "mouse", "key", "windows"})

    def test_cli_requires_out_and_secs(self) -> None:
        r = subprocess.run([sys.executable, str(TOOL)],
                           capture_output=True, text=True,
                           encoding="utf-8", timeout=30)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
