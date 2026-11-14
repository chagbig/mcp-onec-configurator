# -*- coding: utf-8 -*-
"""Скриншот окна конфигуратора (№24): PrintWindow по pid + подстроке заголовка.

Сторона станции (ctypes + Pillow) — мост не участвует: GDI-вызовы из тика
моста несовместимы с инвариантом «мост только Named-операции».

Порядок: list_windows(session) → выбрать окно → window_shot(...).
PrintWindow с PW_RENDERFULLCONTENT (2) захватывает содержимое даже если окно
перекрыто; классические окна V8 рендерятся корректно (проверено на конфигураторе).
"""
from __future__ import annotations

import ctypes
import io
import os
from ctypes import wintypes
from pathlib import Path

import win32gui  # pywin32 (уже зависимость сервера)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD),
                ('biWidth', wintypes.LONG),
                ('biHeight', wintypes.LONG),
                ('biPlanes', wintypes.WORD),
                ('biBitCount', wintypes.WORD),
                ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD),
                ('biXPelsPerMeter', wintypes.LONG),
                ('biYPelsPerMeter', wintypes.LONG),
                ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER),
                ('bmiColors', wintypes.DWORD * 3)]


def _pid_windows(pid: int) -> list:
    """Видимые окна верхнего уровня процесса: [{hwnd, title, rect}]"""
    out = []

    def cb(hwnd, _):
        pid_out = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_out))
        if pid_out.value != pid or not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        rect = wintypes.RECT()
        if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            out.append({'hwnd': hwnd, 'title': title,
                        'rect': (rect.left, rect.top, rect.right, rect.bottom)})
        return True

    win32gui.EnumWindows(cb, 0)
    return out


def list_windows(pid: int) -> list:
    """Публичный список окон 1cv8 для выбора целевого."""
    return _pid_windows(pid)


def shot_window(hwnd: int, out_file: str) -> dict:
    """PrintWindow выбранного hwnd → PNG. Возвращает {out_file, width, height}."""
    from PIL import ImageGrab  # noqa: F401 — фактический захват через Pillow ниже

    user32 = ctypes.windll.user32
    gdi = ctypes.windll.gdi32

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError('GetWindowRect failed')
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise OSError(f'пустой размер окна: {w}x{h}')

    hdc_window = user32.GetDC(hwnd)
    hdc_mem = gdi.CreateCompatibleDC(hdc_window)
    bmp = gdi.CreateCompatibleBitmap(hdc_window, w, h)
    gdi.SelectObject(hdc_mem, bmp)
    PW_RENDERFULLCONTENT = 2
    ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)

    # BMP → PNG через Pillow: собрать сырые биты (BITMAPINFOHEADER 40 байт)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)

    from PIL import Image
    img = Image.frombuffer('RGBX', (w, h), buf.raw, 'raw', 'RGBX', 0, 1)
    img = img.convert('RGB')  # отбросить альфа-байт
    out_file = os.path.abspath(out_file)
    os.makedirs(os.path.dirname(out_file) or '.', exist_ok=True)
    img.save(out_file, 'PNG')

    gdi.DeleteObject(bmp)
    gdi.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_window)

    if not ok and img.getextrema() == ((0, 0), (0, 0), (0, 0)):
        raise OSError('PrintWindow отдал пустой чёрный кадр')
    return {'out_file': out_file, 'width': w, 'height': h}


def shot_by_title(pid: int, title_contains: str, out_file: str) -> dict:
    """Скриншот окна процесса по подстроке заголовка (без учёта регистра)."""
    wins = _pid_windows(pid)
    matches = [w for w in wins
               if title_contains.lower() in w['title'].lower()]
    if not matches:
        listing = '\n'.join(f"  {w['title'][:80]}" for w in wins)
        raise FileNotFoundError(
            f'окно с «{title_contains}» не найдено у pid {pid}. Видимые:\n{listing}')
    # приоритет: самое длинное совпадающее название (конкретнее)
    best = max(matches, key=lambda w: len(w['title']))
    res = shot_window(best['hwnd'], out_file)
    res.update({'title': best['title'], 'rect': best['rect']})
    return res
