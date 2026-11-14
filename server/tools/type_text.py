# -*- coding: utf-8 -*-
"""UNICODE-текстовый ввод для выделения строки в списках конфигуратора (№28).

Проблема: списки 1С (например, менеджер «Расширения конфигурации») ищут строку
по вводимым символам, а keybd_event(VK) требует правильной раскладки — русские
буквы через виртуальные клавиши не вводятся. Правильный механизм — SendInput
с флагом KEYEVENTF_UNICODE: каждый wchar посылается как scan-код (wVk=0,
wScan=wch), раскладка не важна.

Состав:
- build_unicode_inputs(text) — ЧИСТАЯ функция: текст → события down/up
  [{wch, keyup}]; символы вне BMP (ord > 0xFFFF) — surrogate pair
  (две пары: high+low surrogate);
- send_unicode(text, delay_ms) — Windows-only: SendInput посимвольно
  (down+up, пауза delay_ms мс); вне win32 — честный RuntimeError.

Safety: управляющие символы (ord < 0x20, включая Enter/Tab) не посылаются —
иначе вместо «выделить строку поиском по вводу» можно случайно подтвердить
диалог. Кириллица/латиница/цифры — ок.

Живой сценарий (стенд владельца): фокус в списке менеджера «Расширения
конфигурации» → send_unicode("ИмяРасширения") → строка выделяется
штатным поиском по вводимым символам.
"""
from __future__ import annotations

import ctypes
import struct
import sys
import time
from ctypes import wintypes

KEYEVENTF_UNICODE = 0x4000
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD),                # 0 для UNICODE-ввода
                ("wScan", wintypes.WORD),              # wchar символа
                ("dwFlags", wintypes.DWORD),           # UNICODE [+ KEYUP]
                ("time", wintypes.DWORD),              # 0 — системное время
                ("dwExtraInfo", ctypes.c_size_t)]      # ULONG_PTR


class _MOUSEINPUT(ctypes.Structure):
    # нужен только как самый крупный член union INPUT (размер/выравнивание)
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUT_UNION)]


def count_skipped(text: str) -> int:
    """Сколько символов будет отброшено как управляющие (ord < 0x20)."""
    return sum(1 for ch in text if ord(ch) < 0x20)


def flags_for(keyup: bool) -> int:
    """dwFlags события: KEYEVENTF_UNICODE (+KEYEVENTF_KEYUP для отпускания)."""
    return KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if keyup else 0)


def build_unicode_inputs(text: str) -> list[dict]:
    """ЧИСТАЯ функция: текст → список событий [{wch, keyup}].

    На каждый посылаемый символ — пара down+up с KEYEVENTF_UNICODE;
    символы вне BMP разворачиваются в surrogate pair (две пары:
    high+low surrogate × down/up);
    управляющие (ord < 0x20, включая Enter/Tab) пропускаются.
    """
    events: list[dict] = []
    for ch in text:
        if ord(ch) < 0x20:
            continue  # safety: Enter/Tab и прочие управляющие не посылать
        # сырые UTF-16 code units: BMP-символ — один юнит, вне BMP — пара
        # (decode здесь не годится: он обратно склеивает surrogate pair)
        raw = ch.encode("utf-16-le")
        for wch in struct.unpack("<%dH" % (len(raw) // 2), raw):
            events.append({"wch": wch, "keyup": False})  # down
            events.append({"wch": wch, "keyup": True})   # up
    return events


def _make_send_input():
    """Windows-only: SendInput из user32 с типизированной сигнатурой."""
    fn = ctypes.windll.user32.SendInput
    fn.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    fn.restype = wintypes.UINT
    return fn


def send_unicode(text: str, delay_ms: int = 30) -> dict:
    """Посимвольный UNICODE-ввод в текущий фокус. Windows-only.

    Вне win32 — RuntimeError «ввод только на Windows». Пустой текст и текст
    из одних управляющих ничего не вводит. Возврат {sent, failed, skipped}:
    sent — реально посланные символы, failed — символы, на которых SendInput
    вернул 0, skipped — отброшенные управляющие.
    """
    if sys.platform != "win32":
        raise RuntimeError("ввод только на Windows")
    skipped = count_skipped(text)
    sendable = [ch for ch in text if ord(ch) >= 0x20]
    if not sendable:
        return {"sent": 0, "failed": 0, "skipped": skipped}
    send_input = _make_send_input()
    size = ctypes.sizeof(INPUT)
    sent = failed = 0
    for ch in sendable:
        evs = build_unicode_inputs(ch)
        arr = (INPUT * len(evs))()
        for i, ev in enumerate(evs):
            arr[i].type = INPUT_KEYBOARD
            arr[i].union.ki = KEYBDINPUT(
                wVk=0, wScan=ev["wch"], dwFlags=flags_for(ev["keyup"]),
                time=0, dwExtraInfo=0)
        if send_input(len(evs), arr, size) == 0:
            failed += 1
        else:
            sent += 1
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
    return {"sent": sent, "failed": failed, "skipped": skipped}
