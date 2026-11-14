"""Активация окна конфигуратора перед операциями дерева (этап 7, ловушка фокуса).

Нажатия клавиш мост шлёт в окно фокуса ввода (winApi.getFocus()); hwnd представлений
в SnegAPI на 8.3.12+ всегда 0. Если конфигуратор в фоне, фокуса нет — сервер поднимает
его окно (EnumWindows по pid -> SetForegroundWindow с разблокировкой через Alt).

Windows-only: на Linux (CI) import не должен падать — user32 регистрируется лениво.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

GW_OWNER = 4
SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

user32 = None  # инициализация в _ensure_user32()


def _ensure_user32():
    """Ленивая инициализация user32 + сигнатуры (Windows-only)."""
    global user32
    if user32 is not None:
        return
    user32 = ctypes.windll.user32
    # явные сигнатуры: без них ctypes трактирует hwnd как int и падает на 64-битных дескрипторах
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, wintypes.INT]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    user32.keybd_event.restype = None


def _enum_cb(hwnd: int, _lparam: int) -> bool:
    """Обычная функция — WINFUNCTYPE-обёртка создаётся в main_window (Linux-safe)."""
    wpid = wintypes.DWORD()
    _ensure_user32()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
    if wpid.value == _enum_cb.pid and user32.IsWindowVisible(hwnd):  # type: ignore[attr-defined]
        _enum_cb.hwnds.append(hwnd)  # type: ignore[attr-defined]
    return True


def main_window(pid: int) -> int | None:
    _ensure_user32()
    """Видимое окно верхнего уровня процесса (без владельца), либо любое видимое."""
    _enum_cb.pid = pid  # type: ignore[attr-defined]
    _enum_cb.hwnds = []  # type: ignore[attr-defined]
    # WINFUNCTYPE — Windows-only (на Linux CFUNCTYPE, но EnumWindows тоже Windows-only)
    cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_enum_cb)
    user32.EnumWindows(cb, 0)
    if not _enum_cb.hwnds:  # type: ignore[attr-defined]
        return None
    for hwnd in _enum_cb.hwnds:  # type: ignore[attr-defined]
        if not user32.GetWindow(hwnd, GW_OWNER):
            return hwnd
    return _enum_cb.hwnds[0]  # type: ignore[attr-defined]


def activate_window(pid: int, wait_s: float = 1.5) -> bool:
    """Сделать окно процесса передним. True, если окно уже переднее или стало им.

    Только Alt-эмуляция + SetForegroundWindow/BringWindowToTop. AttachThreadInput
    и SetFocus из чужого потока НЕ используем: после них создание окон в 1cv8
    зависало (нарушение очереди ввода), 23.10.2026.
    """
    _ensure_user32()
    hwnd = main_window(pid)
    if not hwnd:
        return False
    if user32.GetForegroundWindow() == hwnd:
        return True
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.15)

    deadline = time.time() + wait_s
    while time.time() < deadline:
        # эмуляция Alt — процесс получает право перевести foreground
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.1)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return user32.GetForegroundWindow() == hwnd
