# -*- coding: utf-8 -*-
"""Тесты IDEAS №6 (enterprise_smoke): чистые функции, валидация ИБ, Linux-отказ,
CloseSequence на fake-процессе. Реальный 1cv8c в тестах НЕ запускается."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from enterprise_smoke import (CloseSequence, EnterpriseSmokeError,
                              build_command, check_platform, find_1cv8c,
                              split_extra, validate_base)


class FakeProc:
    """Процесс-фейк с poll(): жив, пока счётчик опросов не дойдёт до exit_after."""
    def __init__(self, pid=4242, exit_after=None):
        self.pid = pid
        self.exit_after = exit_after
        self._polls = 0
        self.code = None

    def poll(self) -> int | None:
        self._polls += 1
        if self.exit_after is not None and self._polls >= self.exit_after:
            self.code = 0
        return self.code


class FakeClock:
    """Управляемое время: sleep двигает часы, иначе цикл _wait_exit не завершится."""
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class TestBuildCommand(unittest.TestCase):
    def test_minimal(self) -> None:
        cmd = build_command(r"C:\p\1cv8c.exe", r"C:\базы\иб")
        self.assertEqual(cmd, [r"C:\p\1cv8c.exe", "ENTERPRISE",
                               "/DisableStartupDialogs", "/F", r"C:\базы\иб"])

    def test_user_only(self) -> None:
        cmd = build_command("x.exe", "b", user="Админ")
        self.assertEqual(cmd, ["x.exe", "ENTERPRISE", "/DisableStartupDialogs",
                               "/F", "b", "/N", "Админ"])

    def test_user_password(self) -> None:
        cmd = build_command("x.exe", "b", user="Админ", password="секрет")
        self.assertEqual(cmd[-4:], ["/N", "Админ", "/P", "секрет"])

    def test_password_without_user_is_ignored(self) -> None:
        # пароль без пользователя — операция бессмысленна, /P не ставим
        cmd = build_command("x.exe", "b", password="пароль")
        self.assertNotIn("/P", cmd)

    def test_extra_args(self) -> None:
        cmd = build_command("x.exe", "b", extra_args="/AppAutoCheckMode /L ru")
        self.assertEqual(cmd[-3:], ["/AppAutoCheckMode", "/L", "ru"])

    def test_extra_args_quoted_path(self) -> None:
        cmd = build_command("x.exe", "b", extra_args=r'"/Execute" "C:\макет.epf"')
        self.assertEqual(cmd[-2:], ["/Execute", r"C:\макет.epf"])

    def test_empty_password_omitted(self) -> None:
        cmd = build_command("x.exe", "b", user="Иванов", password="")
        self.assertEqual(cmd[-2:], ["/N", "Иванов"])

    def test_split_extra(self) -> None:
        self.assertEqual(split_extra(""), [])
        self.assertEqual(split_extra("   "), [])
        self.assertEqual(split_extra("/A /B  /C"), ["/A", "/B", "/C"])
        self.assertEqual(split_extra(r'"C:\Program Files\x"'), [r"C:\Program Files\x"])


class TestValidateBase(unittest.TestCase):
    def test_missing_dir(self) -> None:
        with self.assertRaises(EnterpriseSmokeError) as cm:
            validate_base(r"C:\совсем\нет")
        self.assertIn("каталог ИБ", str(cm.exception))

    def test_dir_without_1cd(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(EnterpriseSmokeError) as cm:
                validate_base(d)
            self.assertIn("1Cv8.1CD", str(cm.exception))

    def test_valid_base_returns_abspath(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "1Cv8.1CD"), "wb") as f:
                f.write(b"")
            res = validate_base(d)
            self.assertTrue(os.path.isabs(res))
            self.assertEqual(os.path.normcase(res), os.path.normcase(d))


class TestPlatformAndFind(unittest.TestCase):
    def test_linux_refusal(self) -> None:
        with self.assertRaises(EnterpriseSmokeError) as cm:
            check_platform("linux")
        msg = str(cm.exception)
        self.assertIn("Windows", msg)
        self.assertIn("платформа", msg)

    def test_win32_ok(self) -> None:
        check_platform("win32")  # не бросает

    def test_find_no_dir_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            empty = Path(d) / "1cv8"
            empty.mkdir()
            with self.assertRaises(EnterpriseSmokeError) as cm:
                find_1cv8c(empty)
            self.assertIn("1cv8c", str(cm.exception))

    def test_find_missing_root_clear_error(self) -> None:
        with self.assertRaises(EnterpriseSmokeError):
            find_1cv8c(Path(r"C:\нет\1cv8"))

    def test_find_picks_newest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for v, exe in (("8.3.20", "1cv8c.exe"), ("8.3.27", "1cv8c.exe"),
                           ("8.3.27-old", "1cv8.exe")):
                b = root / v / "bin"
                b.mkdir(parents=True)
                (b / exe).write_text("", encoding="utf-8")
            res = find_1cv8c(root)
            self.assertIn("8.3.27", res)
            self.assertTrue(res.endswith("1cv8c.exe"))
            # каталог с одним дизайнером не валидируется как клиентский сеанс
            self.assertNotIn("8.3.27-old", res)


@unittest.skipUnless(sys.platform == "win32",
                     "запуск/окна — Windows-only (на Linux CI честный отказ)")
class TestWindowsLaunchGuard(unittest.TestCase):
    def test_missing_base_rejected_before_popen(self) -> None:
        # валидация до запуска процесса: ничего не стартует
        from enterprise_smoke import launch_enterprise
        with self.assertRaises(EnterpriseSmokeError) as cm:
            launch_enterprise(r"C:\нет\иб")
        self.assertIn("каталог ИБ", str(cm.exception))


class TestCloseSequence(unittest.TestCase):
    def _seq(self, proc, kill_log=None, **kw) -> CloseSequence:
        clock = FakeClock()
        kills = kill_log if kill_log is not None else []

        def taskkill(pid, force):
            kills.append((pid, force))

        posted = []

        def post_close(hwnd):
            posted.append(hwnd)

        kw.setdefault("clock", clock)
        kw.setdefault("sleep", clock.sleep)
        kw.setdefault("post_close", post_close)
        kw.setdefault("taskkill_fn", taskkill)
        seq = CloseSequence(proc, hwnd=101, **kw)
        seq._posted = posted      # для проверок
        seq._kills = kills
        return seq

    def test_grace_wm_close(self) -> None:
        proc = FakeProc(pid=111, exit_after=2)
        seq = self._seq(proc, grace_s=10, poll_s=1.0)
        res = seq.run()
        self.assertTrue(res["closed"])
        self.assertFalse(res["forced"])
        self.assertEqual(res["stage"], "wm_close")
        self.assertEqual(seq._posted, [101])
        self.assertEqual(seq._kills, [])

    def test_taskkill_without_force(self) -> None:
        # WM_CLOSE не помогает, taskkill без /F завершает
        proc = FakeProc(pid=222)
        seq = self._seq(proc, grace_s=2, kill_s=2, poll_s=1.0)
        kills = seq._kills
        orig = seq.taskkill

        def taskkill_after_grace(pid, force):
            orig(pid, force)
            proc.code = 0  # taskkill сработал

        seq.taskkill = taskkill_after_grace
        res = seq.run()
        self.assertTrue(res["closed"])
        self.assertFalse(res["forced"])
        self.assertEqual(res["stage"], "taskkill")
        self.assertEqual(kills, [(222, False)])

    def test_force_after_taskkill(self) -> None:
        proc = FakeProc(pid=333)
        seq = self._seq(proc, grace_s=1, kill_s=1, poll_s=1.0)
        kills = seq._kills
        orig = seq.taskkill
        state = {"n": 0}

        def taskkill_on_force(pid, force):
            orig(pid, force)
            state["n"] += 1
            if force:
                proc.code = 0

        seq.taskkill = taskkill_on_force
        res = seq.run()
        self.assertTrue(res["closed"])
        self.assertTrue(res["forced"])
        self.assertEqual(res["stage"], "force")
        self.assertEqual(kills, [(333, False), (333, True)])

    def test_never_dies_reported(self) -> None:
        proc = FakeProc(pid=444)  # жив вечно
        seq = self._seq(proc, grace_s=1, kill_s=1, poll_s=1.0)
        res = seq.run()
        self.assertFalse(res["closed"])
        self.assertTrue(res["forced"])
        self.assertEqual(res["stage"], "still_alive")
        self.assertEqual(seq._kills, [(444, False), (444, True)])

    def test_no_hwnd_skips_post(self) -> None:
        proc = FakeProc(pid=555, exit_after=1)
        seq = self._seq(proc, grace_s=10, poll_s=1.0)
        seq.hwnd = None
        res = seq.run()
        self.assertEqual(seq._posted, [])
        self.assertTrue(res["closed"])

    def test_post_close_exception_not_fatal(self) -> None:
        def bad_post(hwnd):
            raise OSError("окно исчезло")

        proc = FakeProc(pid=666, exit_after=1)
        seq = self._seq(proc, grace_s=10, poll_s=1.0, post_close=bad_post)
        res = seq.run()
        self.assertTrue(res["closed"])
        self.assertEqual(res["stage"], "wm_close")


if __name__ == "__main__":
    unittest.main()
