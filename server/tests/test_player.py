# -*- coding: utf-8 -*-
"""player (v1.17 «Сделай как я»): классификация лога, replay-план, аудит.

Синтетический JSONL user_watch (клики/клавиши/окна) во временных файлах.
Проверяется: три класса принципа «лог → снегопат-путь» (Insert/Delete в
окне конфигуратора -> covered_op с op моста; клик в полосе меню ->
menu_invoke; прочее -> uncovered; up наследует класс парного down; карта
--map расширяет vk_ops/menu_titles); coverage-сводка и md-обогащение
(uncovered с фактом журнала моста в окне); dry-run replay НЕ зовёт
send-функции; исполнение (--yes, без --dry-run) зовёт инъекции в порядке
план-шагов ПОСЛЕ гварда foreground (мимо маркера — отказ и ноль ввода);
исполнение с заглушками запрещено; CLI rc=0/2 и запуск python -I.
Реальные исполнители (ctypes keybd_event) в тестах НЕ вызываются.
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import player  # noqa: E402
from player import (CLASSES, DEFAULT_COVERAGE_MAP, VK_DELETE, VK_INSERT,  # noqa: E402
                    build_click_sender, check_foreground, classify_events,
                    classify_event, class_stats, coverage_report,
                    event_title, load_coverage_map, load_user_log,
                    menu_name_for, replay)

TOOL = Path(__file__).resolve().parents[1] / "tools" / "player.py"

CONF = "БазаВ1 — Конфигуратор"   # заголовок с маркером конфигуратора


# --- фикстуры ---------------------------------------------------------------


def _click(iso: str, x: int = 150, y: int = 20, title: str = CONF,
           ev: str = "down") -> dict:
    return {"t": iso, "type": "mouse", "ev": ev, "x": x, "y": y,
            "vk": None, "fg_title": title}


def _key(iso: str, vk: int, ev: str = "down", title: str = CONF) -> dict:
    return {"t": iso, "type": "key", "vk": vk, "ev": ev, "fg_title": title}


def _win(iso: str, title: str) -> dict:
    return {"t": iso, "type": "window", "ev": "fg_change", "hwnd": 7,
            "title": title}


def _mini() -> list:
    """Мини-сценарий: клик по меню, клик по дереву, Insert в дереве."""
    return [_click("2026-11-15T12:00:01", x=150, y=20),
            _click("2026-11-15T12:00:02", x=400, y=300),
            _key("2026-11-15T12:00:03", VK_INSERT)]


def _scenario() -> list:
    """Полный сценарий: все типы событий и ev; up идёт сразу за down."""
    return [
        _click("2026-11-15T12:00:01", x=150, y=20),        # 0 меню
        _click("2026-11-15T12:00:02", x=400, y=300),       # 1 дерево
        _key("2026-11-15T12:00:03", VK_INSERT),            # 2 Insert
        _key("2026-11-15T12:00:03.500", VK_INSERT, ev="up"),  # 3 парный up
        _key("2026-11-15T12:00:04", VK_DELETE),            # 4 Delete
        _key("2026-11-15T12:00:05", 0x41),                 # 5 клавиша 'A'
        _win("2026-11-15T12:00:06", "Создание объекта"),   # 6 диалог
        _click("2026-11-15T12:00:07", x=10, y=10, ev="wheel"),  # 7 колесо
    ]


def _user(path: Path, events: list) -> str:
    """Записать user-лог (utf-8 JSONL), вернуть путь строкой."""
    text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _ms(iso: str) -> int:
    """ISO-метка -> epoch-миллисекунды (наивная = локальная, как в parse_ts)."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp() * 1000)


def _md(path: Path, records: list) -> str:
    """Записать журнал моста (UTF-16 с BOM JSONL — формат моста)."""
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    path.write_bytes(text.encode("utf-16"))
    return str(path)


def _fact(kind_name: str, iso: str, name: str = "МойСправочник") -> dict:
    """Запись журнала моста: ts — epoch-мс (как пишет мост)."""
    return {"ts": _ms(iso), "pid": 4321, "src": "human", "kind": 0,
            "kindName": kind_name, "request": False,
            "obj": {"id": "abc", "name": name, "cls": "Справочник"},
            "container": "0"}


# --- вспомогательные чистые функции -----------------------------------------


class TestEventTitle(unittest.TestCase):
    """Заголовок окна: window -> title, mouse/key -> fg_title, нет -> ''. """

    def test_mouse_key_fg_title_window_title(self) -> None:
        self.assertEqual(event_title(_click("2026-11-15T12:00:01")), CONF)
        self.assertEqual(event_title(_key("2026-11-15T12:00:01", 0x41)), CONF)
        self.assertEqual(event_title(_win("2026-11-15T12:00:01", "Диалог")),
                         "Диалог")

    def test_missing_title_empty(self) -> None:
        self.assertEqual(event_title({"type": "mouse"}), "")
        self.assertEqual(event_title({"type": "window", "title": 5}), "")


class TestMenuName(unittest.TestCase):
    """menu_name_for: канон по словарю заголовков; не найдено — ''."""

    def test_known_titles(self) -> None:
        cmap = DEFAULT_COVERAGE_MAP
        self.assertEqual(menu_name_for("Конфигуратор Файл", cmap), "file")
        self.assertEqual(menu_name_for("Меню Снегопат", cmap), "snegopat")
        self.assertEqual(menu_name_for("Конфигурация", cmap), "configuration")

    def test_configurator_title_has_no_menu_name(self) -> None:
        # «Конфигуратор» не содержит ни одного имени меню
        self.assertEqual(menu_name_for(CONF, DEFAULT_COVERAGE_MAP), "")


# --- классификация ----------------------------------------------------------


class TestClassify(unittest.TestCase):
    """Три класса принципа «лог → снегопат-путь»; наследование up."""

    def test_insert_delete_covered_with_ops(self) -> None:
        res = classify_events([_key("2026-11-15T12:00:01", VK_INSERT),
                               _key("2026-11-15T12:00:02", VK_DELETE)])
        self.assertEqual([r["class"] for r in res],
                         ["covered_op", "covered_op"])
        self.assertEqual(res[0]["suggest"], {"op": "md_add_child"})
        self.assertEqual(res[1]["suggest"], {"op": "md_delete"})
        # схема записи классификации зафиксирована
        for r in res:
            self.assertEqual(set(r),
                             {"t", "type", "title", "class", "suggest", "why"})
            self.assertIn("md_", r["why"])

    def test_insert_outside_configurator_uncovered(self) -> None:
        ev = _key("2026-11-15T12:00:01", VK_INSERT, title="Блокнот")
        res = classify_event(ev)
        self.assertEqual(res["class"], "uncovered")
        self.assertEqual(res["suggest"], {})
        self.assertIn("Конфигура", res["why"])

    def test_menu_click_in_band(self) -> None:
        res = classify_event(_click("2026-11-15T12:00:01", x=150, y=20))
        self.assertEqual(res["class"], "menu_invoke")
        self.assertIn("menu", res["suggest"])
        self.assertEqual(res["suggest"]["menu"], "")  # имя пункта в логе нет

    def test_menu_name_from_title_when_present(self) -> None:
        ev = _click("2026-11-15T12:00:01", x=90, y=10,
                    title="Конфигуратор Файл")
        res = classify_event(ev)
        self.assertEqual(res["class"], "menu_invoke")
        self.assertEqual(res["suggest"]["menu"], "file")

    def test_click_below_band_uncovered(self) -> None:
        res = classify_event(_click("2026-11-15T12:00:01", x=400, y=300))
        self.assertEqual(res["class"], "uncovered")
        self.assertIn("IDEAS", res["why"])

    def test_click_outside_configurator_uncovered(self) -> None:
        ev = _click("2026-11-15T12:00:01", x=100, y=10, title="Проводник")
        self.assertEqual(classify_event(ev)["class"], "uncovered")

    def test_window_dialog_uncovered(self) -> None:
        res = classify_event(_win("2026-11-15T12:00:01", "Создание объекта"))
        self.assertEqual(res["class"], "uncovered")
        self.assertIn("диалог", res["why"])

    def test_wheel_and_unknown_type_uncovered(self) -> None:
        self.assertEqual(classify_event(
            _click("2026-11-15T12:00:01", ev="wheel"))["class"], "uncovered")
        self.assertEqual(classify_event(
            {"t": "2026-11-15T12:00:01", "type": "printer"})["class"],
            "uncovered")

    def test_up_inherits_down_class(self) -> None:
        res = classify_events([_key("2026-11-15T12:00:03", VK_INSERT),
                               _key("2026-11-15T12:00:03.500", VK_INSERT,
                                    ev="up"),
                               _click("2026-11-15T12:00:04", y=20),
                               _click("2026-11-15T12:00:04.500", y=20,
                                      ev="up")])
        self.assertEqual([r["class"] for r in res],
                         ["covered_op", "covered_op", "menu_invoke",
                          "menu_invoke"])
        self.assertTrue(res[1]["why"].startswith("парный up"))

    def test_scenario_classes(self) -> None:
        res = classify_events(_scenario())
        self.assertEqual([r["class"] for r in res],
                         ["menu_invoke", "uncovered", "covered_op",
                          "covered_op", "covered_op", "uncovered",
                          "uncovered", "uncovered"])

    def test_custom_map_extends(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mf = Path(td) / "map.json"
            mf.write_text(json.dumps(
                {"vk_ops": {"13": "config_save"},
                 "menu_area_max_y": 400,
                 "menu_titles": {"Сервис": "service"}}, ensure_ascii=False),
                encoding="utf-8")
            cmap = load_coverage_map(str(mf))
        # дефолты сохранены, новые данные добавлены
        self.assertEqual(cmap["vk_ops"]["45"], "md_add_child")
        self.assertEqual(cmap["vk_ops"]["13"], "config_save")
        self.assertEqual(cmap["menu_titles"]["Файл"], "file")
        self.assertEqual(cmap["menu_titles"]["Сервис"], "service")
        # Enter (vk=13) в конфигураторе -> covered_op с op из карты
        res = classify_event(_key("2026-11-15T12:00:01", 0x0D), cmap)
        self.assertEqual(res["class"], "covered_op")
        self.assertEqual(res["suggest"], {"op": "config_save"})
        # y=300 теперь внутри расширенной полосы меню
        res = classify_event(_click("2026-11-15T12:00:02", y=300), cmap)
        self.assertEqual(res["class"], "menu_invoke")

    def test_load_coverage_map_bad_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mf = Path(td) / "bad.json"
            mf.write_text("{обрыв", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_coverage_map(str(mf))
            mf.write_text("[1, 2]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_coverage_map(str(mf))


class TestClassStats(unittest.TestCase):
    def test_keys_all_classes(self) -> None:
        self.assertEqual(set(class_stats([])), set(CLASSES))
        res = class_stats(classify_events(_scenario()))
        self.assertEqual(res, {"covered_op": 3, "menu_invoke": 1,
                               "uncovered": 4})


# --- load_user_log ----------------------------------------------------------


class TestLoadUserLog(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def test_sorted_and_broken_counted(self) -> None:
        p = self.d / "u.jsonl"
        p.write_text("мусор\n"
                     + json.dumps(_click("2026-11-15T12:00:02")) + "\n"
                     + "{\"t\": \"битая\", \"type\": \"key\"}\n"
                     + json.dumps(_click("2026-11-15T12:00:01")) + "\n",
                     encoding="utf-8")
        events, diag = load_user_log(str(p))
        self.assertEqual(diag, {"total_lines": 4, "broken_lines": 2})
        self.assertEqual([e["t"] for e in events],
                         ["2026-11-15T12:00:01", "2026-11-15T12:00:02"])

    def test_empty_file_clean(self) -> None:
        p = self.d / "empty.jsonl"
        p.write_text("", encoding="utf-8")
        events, diag = load_user_log(str(p))
        self.assertEqual(events, [])
        self.assertEqual(diag, {"total_lines": 0, "broken_lines": 0})


# --- coverage ---------------------------------------------------------------


class TestCoverage(unittest.TestCase):
    """Аудит покрытия: сводка, uncovered с координатами, md-обогащение."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.events = [
            _click("2026-11-15T12:00:01", x=150, y=20),   # меню (covered)
            _click("2026-11-15T12:00:02", x=400, y=300),  # дерево (gap)
            _key("2026-11-15T12:00:05", 0x41),            # клавиша (gap)
            _click("2026-11-15T12:00:07", x=10, y=10,
                   ev="wheel"),                           # колесо (gap)
        ]
        self.u = _user(self.d / "u.jsonl", self.events)

    def test_summary_and_uncovered_coordinates(self) -> None:
        res = coverage_report(self.u)
        self.assertEqual(res["total"], 4)
        self.assertEqual(res["stats"], {"covered_op": 0, "menu_invoke": 1,
                                        "uncovered": 3, "broken_lines": 0})
        unc = res["uncovered"]
        self.assertEqual([u["type"] for u in unc],
                         ["mouse", "key", "mouse"])
        self.assertEqual((unc[0]["x"], unc[0]["y"]), (400, 300))
        self.assertIsNone(unc[1]["x"])
        self.assertIsNone(unc[1]["y"])
        self.assertEqual((unc[2]["x"], unc[2]["y"]), (10, 10))
        for u in unc:
            self.assertIn("t", u)
            self.assertIn("why", u)

    def test_md_enrichment(self) -> None:
        m = _md(self.d / "j.jsonl", [
            _fact("AddChild", "2026-11-15T12:00:03"),      # после tree-click
            _fact("PropChanged", "2026-11-15T12:00:03"),   # не TRACKED
            _fact("Changed", "2026-11-15T12:00:09"),       # gap 4.0 / 2.0
        ])
        res = coverage_report(self.u, mdjournal_path=m, window=2.0)
        md = res["md"]
        self.assertEqual(md["md_events"], 3)
        self.assertEqual(md["md_tracked"], 2)
        self.assertEqual(md["md_confirmed"], 2)  # 1.0 и ровно 2.0 (включительно)
        unc = res["uncovered"]
        self.assertEqual(unc[0]["md_followed_s"], 1.0)
        self.assertIsNone(unc[1]["md_followed_s"])  # gap 4.0 > window
        self.assertEqual(unc[2]["md_followed_s"], 2.0)

    def test_missing_mdjournal_honest_error(self) -> None:
        missing = str(self.d / "нет-журнала.jsonl")
        with self.assertRaises(ValueError) as cm:
            coverage_report(self.u, mdjournal_path=missing)
        self.assertIn("journal_configure", str(cm.exception))

    def test_negative_window_raises(self) -> None:
        with self.assertRaises(ValueError):
            coverage_report(self.u, window=-1)


# --- replay: dry-run --------------------------------------------------------


class TestReplayDry(unittest.TestCase):
    """Dry-run (по умолчанию): план без единого вызова send-функций."""

    def test_dry_run_plan_structure(self) -> None:
        res = replay(_mini(), session_pid=7)
        self.assertEqual(res["mode"], "dry-run")
        self.assertEqual(res["session_pid"], 7)
        self.assertEqual(res["total_events"], 3)
        self.assertEqual(res["planned"], 3)
        self.assertEqual(res["executed"], 0)
        self.assertEqual(res["sent"], [])
        self.assertEqual(res["foreground"], None)
        actions = [s["action"] for s in res["steps"]]
        self.assertEqual(actions, ["click", "click", "keys"])
        self.assertTrue(all(s["exec"] for s in res["steps"]))
        self.assertEqual(res["steps"][2]["vk"], VK_INSERT)
        self.assertEqual(res["steps"][0]["class"], "menu_invoke")
        self.assertEqual(res["steps"][1]["class"], "uncovered")
        self.assertEqual(res["steps"][2]["class"], "covered_op")

    def test_dry_run_never_calls_senders(self) -> None:
        """Инъекции-шпионы не вызываются ни разу (включая guard/sleep/clock)."""
        spies = {name: mock.MagicMock()
                 for name in ("send_click", "send_keys", "sleep", "clock",
                              "get_foreground")}
        res = replay(_scenario(), dry_run=True, yes=False, **spies)
        self.assertEqual(res["mode"], "dry-run")
        for m in spies.values():
            m.assert_not_called()

    def test_plan_skips_up_and_wheel(self) -> None:
        res = replay(_scenario())
        self.assertEqual(res["planned"], 6)  # 5 down-шагов + 1 window
        kinds = [(s["type"], s["action"], s["exec"]) for s in res["steps"]]
        self.assertEqual(kinds, [
            ("mouse", "click", True), ("mouse", "click", True),
            ("key", "keys", True), ("key", "keys", True),
            ("key", "keys", True), ("window", "none", False)])
        self.assertEqual(res["skipped"], 0)

    def test_max_caps_executable_steps(self) -> None:
        res = replay(_scenario(), max_steps=2)
        exec_steps = [s for s in res["steps"] if s["exec"]]
        self.assertEqual(len(exec_steps), 2)          # первые два клика
        self.assertEqual(res["filtered"]["dropped_max"], 3)
        self.assertEqual(res["skipped"], 3)
        self.assertEqual(res["planned"], 3)           # 2 exec + window-шаг

    def test_start_end_window(self) -> None:
        from graph_builder import parse_ts
        res = replay(_scenario(), start=parse_ts("2026-11-15T12:00:03"))
        self.assertEqual(res["filtered"]["out_of_range"], 2)
        self.assertEqual(res["planned"], 4)  # Insert/Delete/'A'/window
        res = replay(_scenario(), end=parse_ts("2026-11-15T12:00:01.500"))
        self.assertEqual(res["planned"], 1)

    def test_delay_capped_and_first_zero(self) -> None:
        events = [_click("2026-11-15T12:00:01"),
                  _click("2026-11-15T12:01:41")]  # +100 сек
        res = replay(events)
        self.assertEqual(res["steps"][0]["delay_s"], 0.0)
        self.assertEqual(res["steps"][1]["delay_s"], 5.0)  # REPLAY_MAX_GAP_S

    def test_empty_events(self) -> None:
        res = replay([])
        self.assertEqual(res["planned"], 0)
        self.assertEqual(res["mode"], "dry-run")


# --- replay: исполнение -----------------------------------------------------


class TestReplayExecute(unittest.TestCase):
    """--yes без --dry-run: гвард foreground ДО ввода, шаги по порядку."""

    def _run(self, events, fg_title=CONF, **kw):
        calls: list = []

        def fake_fg():
            calls.append(("fg", None))
            return fg_title

        def fake_click(step):
            calls.append(("click", step["i"]))
            return {"ok": True}

        def fake_keys(step):
            calls.append(("keys", step["i"]))
            return {"ok": True}

        def fake_sleep(s):
            calls.append(("sleep", s))

        res = replay(events, session_pid=5, dry_run=False, yes=True,
                     send_click=fake_click, send_keys=fake_keys,
                     sleep=fake_sleep, clock=lambda: 0.0,
                     get_foreground=fake_fg, **kw)
        return res, calls

    def test_execute_order_and_guard_first(self) -> None:
        res, calls = self._run(_mini())
        self.assertEqual(res["mode"], "execute")
        self.assertEqual(res["foreground"], CONF)
        self.assertEqual(res["executed"], 3)
        # порядок: гвард, затем шаги плана (перед 2-м и 3-м — пауза 1.0)
        self.assertEqual(calls, [("fg", None), ("click", 0), ("sleep", 1.0),
                                 ("click", 1), ("sleep", 1.0), ("keys", 2)])
        self.assertEqual([s["i"] for s in res["sent"]], [0, 1, 2])
        self.assertEqual(res["elapsed_s"], 0.0)

    def test_execute_skips_window_steps(self) -> None:
        res, calls = self._run([_key("2026-11-15T12:00:01", VK_INSERT),
                                _win("2026-11-15T12:00:02", "Диалог")])
        self.assertEqual(res["executed"], 1)
        self.assertEqual(calls, [("fg", None), ("keys", 0)])
        self.assertEqual(res["sent"][0]["action"], "keys")

    def test_guard_mismatch_refuses_before_input(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            self._run(_mini(), fg_title="Блокнот — Безымянный")
        self.assertIn("foreground", str(cm.exception))
        self.assertIn("Конфигура", str(cm.exception))

    def test_stubs_refused_in_execute(self) -> None:
        with self.assertRaises(ValueError) as cm:
            replay(_mini(), dry_run=False, yes=True)  # дефолты-заглушки
        self.assertIn("заглушками", str(cm.exception))


class TestGuardsAndSenders(unittest.TestCase):
    """Гвард и фабрики исполнителей (реальный ввод в тестах не зовётся)."""

    def test_check_foreground_ok_and_fail(self) -> None:
        self.assertEqual(check_foreground(lambda: CONF), CONF)
        with self.assertRaises(RuntimeError):
            check_foreground(lambda: "")

    def test_click_sender_refuses_with_bridge_instruction(self) -> None:
        sender = build_click_sender(42)
        with self.assertRaises(RuntimeError) as cm:
            sender({"x": 10, "y": 20})
        msg = str(cm.exception)
        self.assertIn("тиком моста", msg)
        self.assertIn("sessions/42/req", msg)
        self.assertIn("send_click", msg)

    def test_stub_senders_pure(self) -> None:
        out = player._stub_click({"x": 1, "y": 2})
        self.assertEqual(out, {"stub": True, "action": "click", "x": 1, "y": 2})
        out = player._stub_keys({"vk": 45})
        self.assertEqual(out, {"stub": True, "action": "keys", "vk": 45})

    @unittest.skipUnless(sys.platform == "win32",
                         "keybd_event доступен только на Windows")
    def test_keys_sender_builds_callable_win32(self) -> None:
        sender = player.build_keys_sender()
        self.assertTrue(callable(sender))  # НЕ вызываем — реальный ввод

    def test_real_get_foreground_platform_guard(self) -> None:
        if sys.platform == "win32":
            self.assertIsInstance(player.real_get_foreground(), str)
        else:
            with self.assertRaises(RuntimeError):
                player.real_get_foreground()


# --- CLI --------------------------------------------------------------------


class TestCli(unittest.TestCase):
    """CLI: classify/replay/coverage; rc=0 успех, 2 ошибка входа."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.u = _user(self.d / "u.jsonl", _scenario())
        self.broken = self.d / "broken.jsonl"
        self.broken.write_text("мусор\n{\"обрыв\n", encoding="utf-8")

    def test_classify_out_rc0(self) -> None:
        out = self.d / "report.json"
        rc = player.main(["classify", "--user", self.u,
                          "--session-pid", "4321", "--out", str(out)])
        self.assertEqual(rc, 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["session_pid"], 4321)
        self.assertEqual(data["total"], 8)
        self.assertEqual(data["stats"], {"covered_op": 3, "menu_invoke": 1,
                                         "uncovered": 4})
        self.assertEqual(len(data["actions"]), 8)
        for a in data["actions"]:
            self.assertEqual(set(a),
                             {"t", "type", "title", "class", "suggest", "why"})
        self.assertEqual(data["diag"]["broken_lines"], 0)

    def test_classify_stdout_rc0(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = player.main(["classify", "--user", self.u])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["total"], 8)

    def test_classify_missing_file_rc2(self) -> None:
        rc = player.main(["classify", "--user", str(self.d / "нет.jsonl")])
        self.assertEqual(rc, 2)

    def test_classify_broken_jsonl_rc2(self) -> None:
        rc = player.main(["classify", "--user", str(self.broken)])
        self.assertEqual(rc, 2)

    def test_classify_empty_file_rc0(self) -> None:
        p = self.d / "empty.jsonl"
        p.write_text("", encoding="utf-8")
        out = self.d / "empty-report.json"
        rc = player.main(["classify", "--user", str(p), "--out", str(out)])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["total"],
                         0)

    def test_classify_map(self) -> None:
        mf = self.d / "map.json"
        mf.write_text(json.dumps({"vk_ops": {"13": "config_save"}}),
                      encoding="utf-8")
        out = self.d / "rep.json"
        rc = player.main(["classify", "--user", self.u, "--map", str(mf),
                          "--out", str(out)])
        self.assertEqual(rc, 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        # карта дополнилась: Insert по-прежнему md_add_child
        ins = [a for a in data["actions"] if a["type"] == "key"
               and a["class"] == "covered_op"
               and a["suggest"].get("op") == "md_add_child"]
        self.assertTrue(ins)

    def test_classify_bad_map_rc2(self) -> None:
        rc = player.main(["classify", "--user", self.u,
                          "--map", str(self.broken)])
        self.assertEqual(rc, 2)

    def test_replay_default_dry_run_rc0(self) -> None:
        out = self.d / "plan.json"
        rc = player.main(["replay", "--user", self.u, "--session-pid", "4321",
                          "--out", str(out)])
        self.assertEqual(rc, 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "dry-run")
        self.assertEqual(data["executed"], 0)
        self.assertEqual(data["sent"], [])
        self.assertEqual(data["planned"], 6)

    def test_replay_yes_dry_run_flag_wins(self) -> None:
        out = self.d / "plan.json"
        rc = player.main(["replay", "--user", self.u, "--session-pid", "4321",
                          "--yes", "--dry-run", "--out", str(out)])
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(out.read_text(encoding="utf-8"))["mode"], "dry-run")

    def test_replay_yes_executes_with_patched_builders(self) -> None:
        """Шов интеграции: main строит исполнителей фабриками модуля; в
        MCP-обёртке основной агент подставит реальные (здесь — двойники)."""
        clicks, keys, fgs = [], [], []

        def fake_click(step):
            clicks.append(step["i"])
            return {"ok": True}

        def fake_keys(step):
            keys.append(step["i"])
            return {"ok": True}

        def fake_fg():
            fgs.append(1)
            return CONF

        out = self.d / "plan.json"
        u = _user(self.d / "mini.jsonl", _mini())
        with mock.patch.object(player, "build_click_sender",
                               lambda pid: fake_click), \
             mock.patch.object(player, "build_keys_sender",
                               lambda: fake_keys), \
             mock.patch.object(player, "real_get_foreground", fake_fg):
            rc = player.main(["replay", "--user", u, "--session-pid", "9",
                              "--yes", "--out", str(out)])
        self.assertEqual(rc, 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "execute")
        self.assertEqual(data["executed"], 3)
        self.assertEqual(data["foreground"], CONF)
        self.assertEqual(clicks, [0, 1])
        self.assertEqual(keys, [2])
        self.assertEqual(fgs, [1])  # гвард — ровно один раз, до ввода

    def test_replay_cli_direct_injections(self) -> None:
        """replay_cli принимает инъекции напрямую (точка для MCP-обёртки)."""
        clicks, keys = [], []

        args = argparse.Namespace(
            user=self.u, session_pid=9, dry_run=False, yes=True, start="",
            end="", max=0, map="", out="")
        res = player.replay_cli(
            args,
            send_click=lambda s: clicks.append(s["i"]) or {"ok": True},
            send_keys=lambda s: keys.append(s["i"]) or {"ok": True},
            get_foreground=lambda: CONF)
        self.assertEqual(res["mode"], "execute")
        self.assertEqual(res["executed"], 5)
        self.assertEqual(clicks, [0, 1])
        self.assertEqual(keys, [2, 3, 4])

    def test_replay_requires_session_pid(self) -> None:
        with self.assertRaises(SystemExit):
            player.main(["replay", "--user", self.u])

    def test_replay_missing_file_rc2(self) -> None:
        rc = player.main(["replay", "--user", str(self.d / "нет.jsonl"),
                          "--session-pid", "1"])
        self.assertEqual(rc, 2)

    def test_replay_bad_start_rc2(self) -> None:
        rc = player.main(["replay", "--user", self.u, "--session-pid", "1",
                          "--start", "не-дата"])
        self.assertEqual(rc, 2)

    def test_coverage_out_rc0(self) -> None:
        out = self.d / "cov.json"
        rc = player.main(["coverage", "--user", self.u, "--out", str(out)])
        self.assertEqual(rc, 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["total"], 8)
        self.assertEqual(data["stats"]["uncovered"], 4)
        self.assertEqual(len(data["uncovered"]), 4)
        self.assertNotIn("md", data)

    def test_coverage_with_mdjournal_rc0(self) -> None:
        m = _md(self.d / "j.jsonl",
                [_fact("AddChild", "2026-11-15T12:00:02.500")])
        out = self.d / "cov.json"
        rc = player.main(["coverage", "--user", self.u, "--mdjournal", m,
                          "--window", "2", "--out", str(out)])
        self.assertEqual(rc, 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["md"]["md_events"], 1)
        self.assertEqual(data["md"]["md_tracked"], 1)
        # tree-click 12:00:02 -> AddChild 12:00:02.500 (gap 0.5)
        self.assertEqual(data["uncovered"][0]["md_followed_s"], 0.5)

    def test_coverage_missing_mdjournal_rc2(self) -> None:
        rc = player.main(["coverage", "--user", self.u,
                          "--mdjournal", str(self.d / "нет.jsonl")])
        self.assertEqual(rc, 2)

    def test_coverage_missing_user_rc2(self) -> None:
        rc = player.main(["coverage", "--user", str(self.d / "нет.jsonl")])
        self.assertEqual(rc, 2)

    def test_cli_isolated_import_rc0(self) -> None:
        """python -I (изолированный запуск): sys.path.insert в модуле
        делает соседей (graph_builder/journal_dashboard) импортируемыми."""
        out = self.d / "iso.json"
        r = subprocess.run([sys.executable, "-I", str(TOOL), "classify",
                            "--user", self.u, "--out", str(out)],
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["total"],
                         8)


if __name__ == "__main__":
    unittest.main()
