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
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
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


def foreground_pid() -> int | None:
    """pid процесса, владеющего foreground-окном (GetForegroundWindow ->
    GetWindowThreadProcessId); 0/нет — None. #22 (прогон v1.17): диалоги
    конфигуратора («О программе», ошибки) НЕ ставят modalMode, но перехватывают
    foreground — их видно здесь и они не равны main_window(pid)."""
    if sys.platform != "win32":
        return None
    _ensure_user32()
    fg = user32.GetForegroundWindow()
    if not fg:
        return None
    wpid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(fg, ctypes.byref(wpid))
    return wpid.value or None


def foreground_title() -> str:
    """Заголовок foreground-окна (пусто — нет/не Windows). Для диагностики
    отказов modal_open/no_input_focus в ответе инструмента."""
    if sys.platform != "win32":
        return ""
    _ensure_user32()
    fg = user32.GetForegroundWindow()
    if not fg:
        return ""
    n = user32.GetWindowTextLengthW(fg)
    buf = ctypes.create_unicode_buffer(max(1, n + 1))
    user32.GetWindowTextW(fg, buf, n + 1)
    return buf.value


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
