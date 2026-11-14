# -*- coding: utf-8 -*-
"""№28 (type_text): UNICODE-ввод для выделения строки в списках конфигуратора.

Чистые части: build_unicode_inputs — пары down/up на «МсрРасш1», surrogate
pair вне BMP, skip управляющих (Enter/Tab), флаги KEYEVENTF_*; send_unicode —
Windows-гвард (пустой текст без ввода), отказ на Linux (mock sys.platform),
учёт failed при SendInput==0 (mock _make_send_input).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import type_text  # noqa: E402


class TestBuildUnicodeInputs(unittest.TestCase):
    def test_flags_constants(self) -> None:
        self.assertEqual(type_text.KEYEVENTF_UNICODE, 0x4000)
        self.assertEqual(type_text.KEYEVENTF_KEYUP, 0x0002)
        self.assertEqual(type_text.INPUT_KEYBOARD, 1)

    def test_msr_rassh1_down_up_pairs(self) -> None:
        text = "МсрРасш1"
        events = type_text.build_unicode_inputs(text)
        self.assertEqual(len(events), 16)  # 8 символов x (down+up)
        for idx, ch in enumerate(text):
            down, up = events[2 * idx], events[2 * idx + 1]
            self.assertEqual(down["wch"], ord(ch))
            self.assertEqual(up["wch"], ord(ch))
            self.assertFalse(down["keyup"])
            self.assertTrue(up["keyup"])
        # точечные значения: ord('М')=1052, последняя цифра '1'
        self.assertEqual(events[0]["wch"], 1052)
        self.assertEqual(events[-1]["wch"], ord("1"))

    def test_surrogate_pair_beyond_bmp(self) -> None:
        events = type_text.build_unicode_inputs("\U0001F600")  # вне BMP
        self.assertEqual(len(events), 4)  # high+low surrogate x down/up
        self.assertEqual([e["wch"] for e in events],
                         [0xD83D, 0xD83D, 0xDE00, 0xDE00])
        self.assertEqual([e["keyup"] for e in events],
                         [False, True, False, True])

    def test_control_chars_skipped(self) -> None:
        events = type_text.build_unicode_inputs("а\nб")
        self.assertEqual([e["wch"] for e in events],
                         [ord("а"), ord("а"), ord("б"), ord("б")])
        self.assertEqual(type_text.count_skipped("а\nб"), 1)
        self.assertEqual(type_text.count_skipped("\n\t\r"), 3)
        self.assertEqual(type_text.build_unicode_inputs("\n"), [])

    def test_flags_for(self) -> None:
        self.assertEqual(type_text.flags_for(False), 0x4000)
        self.assertEqual(type_text.flags_for(True), 0x4000 | 0x0002)

    def test_empty_text(self) -> None:
        self.assertEqual(type_text.build_unicode_inputs(""), [])


@unittest.skipUnless(sys.platform == "win32",
                     "SendInput доступен только на Windows")
class TestSendUnicodeWindows(unittest.TestCase):
    def test_empty_text_no_input(self) -> None:
        res = type_text.send_unicode("")
        self.assertEqual(res, {"sent": 0, "failed": 0, "skipped": 0})

    def test_only_control_chars_no_input(self) -> None:
        res = type_text.send_unicode("\n\t", delay_ms=0)
        self.assertEqual(res["sent"], 0)
        self.assertEqual(res["skipped"], 2)

    def test_sendinput_zero_counts_failed(self) -> None:
        with mock.patch.object(type_text, "_make_send_input",
                               return_value=lambda n, p, s: 0):
            res = type_text.send_unicode("Мс", delay_ms=0)
        self.assertEqual(res["sent"], 0)
        self.assertEqual(res["failed"], 2)
        self.assertEqual(res["skipped"], 0)


class TestPlatformGuard(unittest.TestCase):
    def test_non_windows_refused(self) -> None:
        with mock.patch.object(sys, "platform", "linux"):
            with self.assertRaises(RuntimeError) as cm:
                type_text.send_unicode("М")
        self.assertIn("ввод только на Windows", str(cm.exception))

    def test_input_struct_sane(self) -> None:
        # INPUT должен вмещать union с KEYBDINPUT (MOUSEINPUT крупнее —
        # поэтому sizeof(INPUT) заметно больше sizeof(KEYBDINPUT))
        self.assertGreater(type_text.ctypes.sizeof(type_text.INPUT),
                           type_text.ctypes.sizeof(type_text.KEYBDINPUT))
        ki = type_text.KEYBDINPUT(wVk=0, wScan=1052,
                                  dwFlags=type_text.KEYEVENTF_UNICODE,
                                  time=0, dwExtraInfo=0)
        self.assertEqual(ki.wVk, 0)
        self.assertEqual(ki.wScan, 1052)
        self.assertEqual(ki.dwFlags, 0x4000)


if __name__ == "__main__":
    unittest.main()
