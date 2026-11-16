# -*- coding: utf-8 -*-
r"""Станционный регистратор действий пользователя (карта переходов, v1.14).

Зачем: пользователь работает в конфигураторе вручную; регистратор пишет его
действия (клики, клавиши, смены foreground-окна) в приватный JSONL-лог.
Параллельный graph_builder сопоставит лог с журналом метаданных моста
(md_journal: UTF-16 JSONL, поля ts/pid/src/kind/kindName/request/obj) и
построит тройки «состояние_до → действие → состояние_после»
(docs/uia-phase0-recon.md, п.2 плана v1.14).

Механика: глобальные низкоуровневые хуки WH_MOUSE_LL(14)/WH_KEYBOARD_LL(13)
(SetWindowsHookExW через ctypes) на пульте сообщений станционного процесса
(GetMessageW/TranslateMessage/DispatchMessageW). Хуки события НЕ глушат —
всегда CallNextHookEx. mouse-move НЕ логируется (только down/up/wheel) —
против флуда. Смена foreground — поллинг WM_TIMER(150 мс) + контроль в
каждом событии; хуки и пульс живут в ОДНОМ потоке (LL-хук требует пульта
сообщений своей нити).

БЕЗОПАСНОСТЬ (критично):
- клавиатура логируется ТОЛЬКО VK-кодами (KBDLLHOOKSTRUCT.vkCode) — БЕЗ
  символов, БЕЗ раскладки, БЕЗ текста: ToUnicode/ToAscii не вызываются,
  WM_CHAR не перехватывается и в лог не пишется; в key-событиях нет поля
  text/char/wch — восстановить ввод по логу невозможно в принципе;
- фильтр мусора: vk < 0x01 и vk == 0xFF не логируются, прочее — как VK;
- мышь — только координаты клика (x/y) и vk=None;
- fg_title — заголовок foreground-окна (метаданные окна, не содержимое
  полей; окно «Соединение с хранилищем» встречается живьём — в лог идут
  только его заголовки, не пароли);
- лог — ПРИВАТНЫЙ JSONL вне репозитория (не коммитить!).

Состав:
- Recorder — чистый аккумулятор: add_event(ev) → список; flush_jsonl(path)
  (UTF-8, ensure_ascii=False, после записи буфер очищается); summary();
- make_mouse_event/make_key_event/make_window_event — чистые конструкторы
  событий (title передаётся явно — тестируемо без Windows);
- install_mouse_hook/install_key_hook/pump_loop — Windows-only (гвард
  sys.platform, вне win32 честный RuntimeError);
- CLI: --pid <конфигуратора> (опц., подсветка fg_pid_match), --out (обязателен),
  --secs N (обязателен); по завершении flush_jsonl + сводка в stdout.

Живой сценарий (владелец, станция с конфигуратором):
  python tools/user_watch.py --pid <pid 1cv8> --out Y:\logs\watch.jsonl --secs 600
  ... работа в конфигураторе ...
  → watch.jsonl + сводка {"events", "mouse", "key", "windows"}
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_TIMER = 0x0113
FG_POLL_MS = 150  # период опроса foreground между событиями ввода

# Сообщения мыши → имя события (move НЕ логируется — флуд)
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0206
WM_MBUTTONUP = 0x0207
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E

# Сообщения клавиатуры → имя события
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

_MOUSE_EV = {
    WM_LBUTTONDOWN: "down", WM_LBUTTONUP: "up",
    WM_RBUTTONDOWN: "down", WM_RBUTTONUP: "up",
    WM_MBUTTONDOWN: "down", WM_MBUTTONUP: "up",
    WM_XBUTTONDOWN: "down", WM_XBUTTONUP: "up",
    WM_MOUSEWHEEL: "wheel", WM_MOUSEHWHEEL: "wheel",
}

_KEY_EV = {
    WM_KEYDOWN: "down", WM_KEYUP: "up",
    WM_SYSKEYDOWN: "down", WM_SYSKEYUP: "up",
}

# ctypes.wintypes.LRESULT в Python 3.13 нет — это то же, что LPARAM
LRESULT = wintypes.LPARAM


class MSLLHOOKSTRUCT(ctypes.Structure):
    """Хук мыши (x64): pt.x+0, pt.y+4, mouseData+8, flags+12, time+16,
    dwExtraInfo+24 (выравнивание 8)."""
    _fields_ = [("pt", wintypes.POINT),
                ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    """Хук клавиатуры: vkCode+0, scanCode+4, flags+8, time+12,
    dwExtraInfo+16 (sizeof 24 на обеих разрядностях)."""
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


user32 = None      # ленивая инициализация в _ensure_user32 (Windows-only)
HOOKPROC = None    # WINFUNCTYPE(LRESULT, nCode, WPARAM, LPARAM)


def _ensure_user32():
    """Ленивая инициализация user32 + сигнатуры (Windows-only).

    Вне win32 — RuntimeError: LL-хуки существуют только на Windows."""
    global user32, HOOKPROC
    if user32 is not None:
        return
    if sys.platform != "win32":
        raise RuntimeError("перехват ввода доступен только на Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                         wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT,
                                ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    user32.KillTimer.restype = wintypes.BOOL


def iso_utc() -> str:
    """Метка времени события: ISO UTC с миллисекундами."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def key_vk_allowed(vk: int) -> bool:
    """Фильтр мусора VK: пропускаем vk < 0x01 и vk == 0xFF, прочее логируем."""
    return 0x01 <= vk <= 0xFE


def make_mouse_event(ev_name: str, x: int, y: int, title: str | None = None) -> dict:
    """Чистый конструктор mouse-события {t, type, ev, x, y, vk=None, fg_title}.

    title=None — взять заголовок foreground-окна (Windows-only); тесты
    передают title явно."""
    if title is None:
        title = fg_title()
    return {"t": iso_utc(), "type": "mouse", "ev": ev_name,
            "x": int(x), "y": int(y), "vk": None, "fg_title": title}


def make_key_event(vk: int, ev_name: str, title: str | None = None) -> dict:
    """Чистый конструктор key-события {t, type, vk, ev, fg_title}.

    ТОЛЬКО VK-код: никаких символов/раскладки/текста — поля text/char/wch
    не существуют, WM_CHAR не перехватывается (см. docstring модуля)."""
    if title is None:
        title = fg_title()
    return {"t": iso_utc(), "type": "key", "vk": int(vk),
            "ev": ev_name, "fg_title": title}


def make_window_event(hwnd: int, title: str) -> dict:
    """Чистый конструктор window-события {t, type, ev=fg_change, hwnd, title}."""
    return {"t": iso_utc(), "type": "window", "ev": "fg_change",
            "hwnd": int(hwnd), "title": title}


def fg_hwnd() -> int:
    """HWND foreground-окна (0 — если нет). Windows-only."""
    _ensure_user32()
    return int(user32.GetForegroundWindow() or 0)


def fg_title() -> str:
    """Заголовок foreground-окна на момент события. Windows-only."""
    hwnd = fg_hwnd()
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _fg_belongs_to(pid: int) -> bool:
    """Foreground-окно принадлежит процессу pid? (подсветка --pid)."""
    hwnd = fg_hwnd()
    if not hwnd:
        return False
    wpid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
    return wpid.value == pid


class Recorder:
    """Чистый аккумулятор событий (без Windows): add_event → список;
    flush_jsonl(path) — UTF-8 JSONL (ensure_ascii=False), после записи
    буфер очищается; summary() — {events, mouse, key, windows}.

    highlight_pid — при заданном pid каждое событие получает булев маркер
    fg_pid_match (foreground принадлежит конфигуратору) — подсветка для
    graph_builder; события при этом не фильтруются."""

    def __init__(self, highlight_pid: int | None = None):
        self.events: list[dict] = []
        self.highlight_pid = highlight_pid

    def add_event(self, ev: dict) -> list[dict]:
        """Добавить событие, вернуть список (однопоточно — хуки и пульс
        живут в одной нити)."""
        if self.highlight_pid is not None and sys.platform == "win32":
            ev = dict(ev, fg_pid_match=_fg_belongs_to(self.highlight_pid))
        self.events.append(ev)
        return self.events

    def flush_jsonl(self, path: str) -> int:
        """Записать события в JSONL (UTF-8, ensure_ascii=False) и очистить
        буфер. Возвращает число записанных строк."""
        count = len(self.events)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for ev in self.events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self.events = []
        return count

    def summary(self) -> dict:
        """Сводка по накопленным событиям {events, mouse, key, windows}."""
        counts = {"mouse": 0, "key": 0, "window": 0}
        for ev in self.events:
            t = ev.get("type")
            if t in counts:
                counts[t] += 1
        return {"events": len(self.events), "mouse": counts["mouse"],
                "key": counts["key"], "windows": counts["window"]}


RECORDER = Recorder()   # приёмник по умолчанию для хуков
_REC: Recorder | None = None   # активный приёмник (ставит install_*)


def parse_kbdl(lparam: int) -> KBDLLHOOKSTRUCT:
    """LPARAM клавиатурного хука → KBDLLHOOKSTRUCT (vkCode в поле .vkCode)."""
    return ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents


def parse_msll(lparam: int) -> MSLLHOOKSTRUCT:
    """LPARAM мышиного хука → MSLLHOOKSTRUCT (координаты в поле .pt)."""
    return ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents


_last_fg: int | None = None   # hwnd последнего зафиксированного foreground


def _track_fg_change() -> None:
    """Зафиксировать смену foreground (window-событие при изменении hwnd)."""
    global _last_fg
    hwnd = fg_hwnd()
    if hwnd == _last_fg:
        return
    _last_fg = hwnd
    if _REC is not None:
        _REC.add_event(make_window_event(hwnd, fg_title()))


def _mouse_proc(nCode, wParam, lParam):
    """LL-хук мыши: событие в лог (down/up/wheel), ввод не глушим."""
    if nCode == HC_ACTION:
        try:
            ev_name = _MOUSE_EV.get(int(wParam))
            if ev_name is not None and _REC is not None:
                ms = parse_msll(int(lParam))
                _REC.add_event(make_mouse_event(ev_name, ms.pt.x, ms.pt.y))
            _track_fg_change()
        except Exception:
            pass  # ошибка логирования не должна рвать цепочку хуков
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


def _key_proc(nCode, wParam, lParam):
    """LL-хук клавиатуры: VK-код в лог (БЕЗ символов), ввод не глушим."""
    if nCode == HC_ACTION:
        try:
            ev_name = _KEY_EV.get(int(wParam))
            if ev_name is not None and _REC is not None:
                kb = parse_kbdl(int(lParam))
                vk = int(kb.vkCode)
                if key_vk_allowed(vk):
                    _REC.add_event(make_key_event(vk, ev_name))
            _track_fg_change()
        except Exception:
            pass
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


_mouse_cb = None    # ссылки на ctypes-callback'и (не отдавать GC)
_key_cb = None
_mouse_hook = None
_key_hook = None


def install_mouse_hook(rec: Recorder | None = None) -> wintypes.HHOOK:
    """Установить WH_MOUSE_LL (Windows-only). Хук и последующий pump_loop
    должны жить в одном потоке."""
    global _REC, _mouse_cb, _mouse_hook
    _ensure_user32()
    _REC = rec if rec is not None else RECORDER
    _mouse_cb = HOOKPROC(_mouse_proc)
    _mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, _mouse_cb, None, 0)
    if not _mouse_hook:
        raise RuntimeError("SetWindowsHookExW(WH_MOUSE_LL) не установлен "
                           f"(GetLastError={ctypes.get_last_error()})")
    return _mouse_hook


def install_key_hook(rec: Recorder | None = None) -> wintypes.HHOOK:
    """Установить WH_KEYBOARD_LL (Windows-only)."""
    global _REC, _key_cb, _key_hook
    _ensure_user32()
    _REC = rec if rec is not None else RECORDER
    _key_cb = HOOKPROC(_key_proc)
    _key_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _key_cb, None, 0)
    if not _key_hook:
        raise RuntimeError("SetWindowsHookExW(WH_KEYBOARD_LL) не установлен "
                           f"(GetLastError={ctypes.get_last_error()})")
    return _key_hook


def unhook_mouse_hook() -> None:
    """Снять мышиный LL-хук (если установлен)."""
    global _mouse_hook
    if _mouse_hook:
        user32.UnhookWindowsHookEx(_mouse_hook)
        _mouse_hook = None


def unhook_key_hook() -> None:
    """Снять клавиатурный LL-хук (если установлен)."""
    global _key_hook
    if _key_hook:
        user32.UnhookWindowsHookEx(_key_hook)
        _key_hook = None


def pump_loop(seconds: float) -> None:
    """Пульт сообщений: GetMessageW/TranslateMessage/DispatchMessageW до
    дедлайна (Windows-only). WM_TIMER каждые FG_POLL_MS будит очередь, чтобы
    между вводом не терять смены foreground. secs <= 0 — ничего не делать."""
    _ensure_user32()
    if float(seconds) <= 0:
        return
    global _last_fg
    _track_fg_change()  # начальное состояние foreground
    deadline = time.monotonic() + float(seconds)
    timer_id = int(user32.SetTimer(None, 0, FG_POLL_MS, None))
    msg = wintypes.MSG()
    try:
        while time.monotonic() < deadline:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:  # WM_QUIT или ошибка
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
            _track_fg_change()
    finally:
        if timer_id:
            user32.KillTimer(None, timer_id)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description='Станционный регистратор действий пользователя '
                    '(VK-коды без символов; приватный JSONL вне репо)')
    ap.add_argument('--pid', type=int, default=0,
                    help='pid конфигуратора — подсветка fg_pid_match')
    ap.add_argument('--out', required=True,
                    help='путь jsonl (приватный, НЕ в репозитории)')
    ap.add_argument('--secs', type=float, required=True,
                    help='длительность записи, сек')
    a = ap.parse_args(argv)
    rec = Recorder(highlight_pid=a.pid or None)
    rc = 0
    try:
        install_mouse_hook(rec)
        install_key_hook(rec)
        pump_loop(a.secs)
    except RuntimeError as e:
        print('user_watch: %s' % e, file=sys.stderr)
        rc = 2
    finally:
        unhook_key_hook()
        unhook_mouse_hook()
    if rc == 0:
        summary = rec.summary()
        rec.flush_jsonl(a.out)
        print(json.dumps(summary, ensure_ascii=False))
    return rc


if __name__ == '__main__':
    sys.exit(main())
