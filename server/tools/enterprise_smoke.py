# -*- coding: utf-8 -*-
"""Enterprise-смоук (IDEAS №6): запуск клиентского сеанса 1cv8c на файловой базе,
ожидание главного окна, скриншот, корректное закрытие — без конфигуратора.

Последний ручной шаг приёмки форм («форма открылась, кнопка на месте»):
launch_enterprise(base) поднимает 1cv8c.exe ENTERPRISE /F <иб>, ждёт появления
видимого окна процесса (заголовок стабилен дважды подряд), снимает PNG через
window_shot.shot_window (PrintWindow, окно может быть перекрыто — фокус не нужен)
и закрывает сеанс каскадом CloseSequence: WM_CLOSE → taskkill /PID → /F /T.

Запуск и сторож — по образцу yaxunit_run (1cv8 ENTERPRISE /F, версия платформы
из каталога C:\\Program Files\\1cv8); окна — через mcp_snegopat.winutils.main_window;
скриншот — переиспользование tools/window_shot.py.

Windows-only: на Linux (CI) — честный отказ «требуется Windows и установленная
платформа»; каталог 1cv8 не найден — явная ошибка. ИБ обязана существовать
(проверка 1Cv8.1CD до запуска — как в yaxunit_run). Реальные прогоны — только
на стенде владельца; тесты покрывают чистые функции и CloseSequence на fake-
процессе и 1cv8c не запускают.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).parent
sys.path.insert(0, str(TOOLS.parent))  # для mcp_snegopat.winutils
sys.path.insert(0, str(TOOLS))         # для window_shot (импортируется лениво)

DEFAULT_PLATFORM_ROOT = Path(r"C:\Program Files\1cv8")
CLIENT_EXE = "1cv8c.exe"          # клиентский сеанс; НЕ 1cv8.exe (дизайнер)
WM_CLOSE = 0x0010
GRACE_S = 10.0                    # ожидание выхода после WM_CLOSE
KILL_S = 5.0                      # ожидание после taskkill без /F
POLL_S = 0.4                      # шаг опроса окна/процесса
TITLE_STABLE_SAMPLES = 2          # сколько опросов подряд заголовок обязан совпасть


class EnterpriseSmokeError(RuntimeError):
    pass


def check_platform(platform: str = sys.platform) -> None:
    """Честный отказ вне Windows: смоук — Windows-only (окна, скриншот, taskkill)."""
    if platform != "win32":
        raise EnterpriseSmokeError(
            "enterprise_smoke: требуется Windows и установленная платформа 1С "
            "(скриншот/окна/taskkill недоступны на %s)" % platform)


def find_1cv8c(root: Path | str = DEFAULT_PLATFORM_ROOT) -> str:
    """Новейший 1cv8c.exe под <root>\\<версия>\\bin (тот же принцип, что find_1cv8
    в yaxunit_run, но клиентский exe). Нет каталога/файла — явная ошибка."""
    root = Path(root)
    best = None
    if root.is_dir():
        for d in root.iterdir():
            exe = d / "bin" / CLIENT_EXE
            if exe.is_file():
                key = [int(x) for x in d.name.split(".") if x.isdigit()]
                if best is None or key > best[0]:
                    best = (key, str(exe))
    if not best:
        raise EnterpriseSmokeError(
            "клиентский сеанс %s не найден под %s (установлена ли платформа 1С?)"
            % (CLIENT_EXE, root))
    return best[1]


def validate_base(base: str) -> str:
    """Абсолютный путь + проверка файловой ИБ (1Cv8.1CD) до запуска — как в yaxunit_run."""
    base = os.path.abspath(base)
    if not os.path.isdir(base):
        raise EnterpriseSmokeError("каталог ИБ не найден: " + base)
    if not os.path.isfile(os.path.join(base, "1Cv8.1CD")):
        raise EnterpriseSmokeError(
            "не похоже на файловую ИБ: нет 1Cv8.1CD в " + base)
    return base


def split_extra(extra_args: str) -> list:
    """Разбор extra_args: shlex без posix-экранирования (Windows-пути с \\ не ломаем),
    парные двойные кавычки снимаются с токена целиком. Пусто → []."""
    if not extra_args or not extra_args.strip():
        return []
    parts = shlex.split(extra_args, posix=False)
    return [p[1:-1] if len(p) > 1 and p[0] == p[-1] == '"' else p for p in parts]


def build_command(exe: str, base: str, user: str | None = None,
                  password: str | None = None, extra_args: str = "") -> list:
    """Команда клиентского сеанса: 1cv8c ENTERPRISE /DisableStartupDialogs /F <иб>
    [/N <пользователь> [/P <пароль>]] [extra]. user/password — опциональны
    (None или пустая строка = без них)."""
    cmd = [exe, "ENTERPRISE", "/DisableStartupDialogs", "/F", base]
    if user:
        cmd += ["/N", user]
        if password:
            cmd += ["/P", password]
    cmd += split_extra(extra_args)
    return cmd


def post_wm_close(hwnd: int) -> None:
    """WM_CLOSE главному окну — штатное закрытие (1C сам сохранит/спросит)."""
    import ctypes
    ctypes.windll.user32.PostMessageW(int(hwnd), WM_CLOSE, 0, 0)


def taskkill(pid: int, force: bool = False) -> subprocess.CompletedProcess:
    """taskkill /PID <pid> (grace) или /F /T (последняя инстанция, включая детей)."""
    cmd = ["taskkill", "/PID", str(pid)]
    if force:
        cmd += ["/F", "/T"]
    return subprocess.run(cmd, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class CloseSequence:
    """Каскад закрытия процесса: WM_CLOSE → taskkill → taskkill /F /T.

    proc — subprocess.Popen или любой объект с poll() и pid (fake в тестах);
    post_close(hwnd) и taskkill(pid, force) — инъекция Windows-действий
    (в тестах — фейки, в продакшене post_wm_close/taskkill). Результат:
    {closed, forced, stage, elapsed_s}; зомби не оставляем (closed=False,
    stage='still_alive' только если не помог даже /F /T).
    """
    def __init__(self, proc, hwnd=None, post_close=None, taskkill_fn=None,
                 grace_s=GRACE_S, kill_s=KILL_S, poll_s=POLL_S,
                 clock=time.monotonic, sleep=time.sleep):
        self.proc = proc
        self.hwnd = hwnd
        self.post_close = post_close if post_close is not None else post_wm_close
        self.taskkill = taskkill_fn if taskkill_fn is not None else taskkill
        self.grace_s = grace_s
        self.kill_s = kill_s
        self.poll_s = poll_s
        self.clock = clock
        self.sleep = sleep

    def _wait_exit(self, timeout_s: float) -> bool:
        deadline = self.clock() + timeout_s
        while self.clock() < deadline:
            if self.proc.poll() is not None:
                return True
            self.sleep(self.poll_s)
        return self.proc.poll() is not None

    def run(self) -> dict:
        t0 = self.clock()
        # 1) вежливо: WM_CLOSE главному окну
        if self.hwnd:
            try:
                self.post_close(self.hwnd)
            except Exception:
                pass  # окно могло уже исчезнуть — не критично
        if self._wait_exit(self.grace_s):
            return {"closed": True, "forced": False, "stage": "wm_close",
                    "elapsed_s": round(self.clock() - t0, 2)}
        # 2) taskkill /PID (без /F)
        self.taskkill(self.proc.pid, False)
        if self._wait_exit(self.kill_s):
            return {"closed": True, "forced": False, "stage": "taskkill",
                    "elapsed_s": round(self.clock() - t0, 2)}
        # 3) taskkill /F /T — в отчёт как forced
        self.taskkill(self.proc.pid, True)
        if self._wait_exit(max(self.kill_s, 2.0)):
            return {"closed": True, "forced": True, "stage": "force",
                    "elapsed_s": round(self.clock() - t0, 2)}
        return {"closed": False, "forced": True, "stage": "still_alive",
                "elapsed_s": round(self.clock() - t0, 2)}


def wait_main_window(pid: int, timeout_s: float = 30.0,
                     poll_s: float = POLL_S) -> tuple:
    """Ждать видимое главное окно процесса с устойчивым заголовком.

    Стабильность: заголовок обязан совпасть в TITLE_STABLE_SAMPLES опросах
    подряд (окно создаётся пустым, потом получает имя базы). Таймаут — ошибка.
    """
    from mcp_snegopat import winutils
    import win32gui
    deadline = time.monotonic() + timeout_s
    last_title = None
    stable = 0
    while time.monotonic() < deadline:
        hwnd = winutils.main_window(pid)
        if hwnd:
            title = win32gui.GetWindowText(hwnd)
            if title:
                if title == last_title:
                    stable += 1
                    if stable >= TITLE_STABLE_SAMPLES:
                        return hwnd, title
                else:
                    stable = 1
                last_title = title
        time.sleep(poll_s)
    raise EnterpriseSmokeError(
        "окно 1cv8c не появилось за %s с (pid %s); возможен диалог входа/ошибки "
        "инициализации" % (timeout_s, pid))


def launch_enterprise(base: str, wait_s: int = 30, shot_file: str = "",
                      close: bool = True, user: str | None = None,
                      password: str | None = None,
                      extra_args: str = "") -> dict:
    """Запустить 1cv8c ENTERPRISE на файловой базе, дождаться окна, снять PNG,
    закрыть каскадом. Вернуть {pid, window_title, shot, closed, forced,
    close_stage, elapsed_s}. close=False — процесс оставлен работать
    (ответственность вызывающего)."""
    check_platform()
    base = validate_base(base)
    exe = find_1cv8c()
    cmd = build_command(exe, base, user, password, extra_args)
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(cmd)
    except OSError as e:
        raise EnterpriseSmokeError("запуск 1cv8c не удался: %s" % e)
    pid = proc.pid
    try:
        hwnd, title = wait_main_window(pid, wait_s)
    except EnterpriseSmokeError:
        taskkill(pid, force=True)  # окно не появилось — не оставлять зомби
        raise

    shot = None
    if shot_file:
        from window_shot import shot_window
        shot = shot_window(hwnd, shot_file)

    res = {"pid": pid, "window_title": title, "shot": shot,
           "closed": False, "forced": False, "close_stage": None}
    if close:
        seq = CloseSequence(proc, hwnd, post_close=post_wm_close,
                            taskkill_fn=taskkill)
        close_res = seq.run()
        res.update({"closed": close_res["closed"], "forced": close_res["forced"],
                    "close_stage": close_res["stage"]})
    res["elapsed_s"] = round(time.monotonic() - t0, 1)
    return res


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    import argparse
    ap = argparse.ArgumentParser(
        description="Enterprise-смоук: 1cv8c на файловой базе + скриншот + закрытие")
    ap.add_argument("--base", required=True, help="файловая ИБ (каталог с 1Cv8.1CD)")
    ap.add_argument("--wait", type=int, default=30, help="сторож ожидания окна, с")
    ap.add_argument("--shot", default="", help="путь PNG-скриншота главного окна")
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--extra-args", default="",
                    help='доп. параметры 1cv8c, например "/AppAutoCheckMode"')
    ap.add_argument("--no-close", action="store_true",
                    help="не закрывать сеанс (процесс останется работать)")
    a = ap.parse_args(argv)
    try:
        res = launch_enterprise(a.base, wait_s=a.wait, shot_file=a.shot,
                                close=not a.no_close, user=a.user,
                                password=a.password, extra_args=a.extra_args)
    except EnterpriseSmokeError as e:
        print("enterprise_smoke: %s" % e, file=sys.stderr)
        return 2
    print(res.get("window_title") or "", res.get("pid"), res.get("elapsed_s"))
    return 0 if res.get("closed") else 1


if __name__ == "__main__":
    sys.exit(main())
