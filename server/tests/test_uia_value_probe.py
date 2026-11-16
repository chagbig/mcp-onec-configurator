# -*- coding: utf-8 -*-
"""uia_value_probe — чистые функции, гварды и структура CLI (v1.17).

РЕАЛЬНЫЙ UIA-СКАН И ЗАПИСЬ В ТЕСТАХ НЕ ЗАПУСКАЮТСЯ (uiaautomation в CI нет,
живая проба гипотезы ValuePattern — на стенде владельца). Проверяются:
- parse_set_arg: «Имя=Текст» -> tuple; ошибки на пустых / без «=» / без имени;
  значение может содержать «=»; пустое значение допустимо (очистка поля);
- is_password_field: «пароль»/«password» в любом регистре -> True;
- summarize: stats {total, edits, with_value_pattern} из фейкового списка;
- match_control / check_target / dry_write_result: match по подстроке
  (case-insensitive), гварды записи (не найдено / пароль / нет паттерна);
- run_set с ФЕЙКОВЫМ паттерном (record/fail stub): SetValue вызывается
  только на валидной цели, ошибка честно в error;
- структура CLI (--pid/--out/--set/--dry-write; rc=2 без --pid,
  rc=2 на --dry-write без --set и на кривом --set);
- честный отказ окружения (rc=2, --out не создаётся, ленивые импорты).

Прогон: python -m unittest discover -s tests -p "test_uia_value_probe.py" -v
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import uia_value_probe  # noqa: E402
from uia_value_probe import (  # noqa: E402
    FIELD_TYPES,
    MAX_CONTROLS,
    MAX_DEPTH,
    NOTE,
    PASSWORD_MARKERS,
    build_report,
    check_target,
    dry_write_result,
    is_password_field,
    main,
    match_control,
    parse_set_arg,
    probe,
    run_set,
    summarize,
)


class _FakePattern:
    """Фейковый ValuePattern: SetValue записывает вызов или бросает ошибку
    (эмуляция живого COM-паттерна без uiautomation)."""

    def __init__(self, fail: bool = False):
        self.calls: list = []
        self.fail = fail

    def SetValue(self, text) -> None:
        if self.fail:
            raise RuntimeError("COM отказ")
        self.calls.append(text)


class TestParseSetArg(unittest.TestCase):
    """parse_set_arg: «ИмяПоля=Текст» -> tuple; ошибки на пустых/без «=»."""

    def test_simple_pair(self) -> None:
        # базовый разбор: имя и текст по первому «=»
        self.assertEqual(parse_set_arg("ИмяПоля=Текст"), ("ИмяПоля", "Текст"))

    def test_first_equals_only(self) -> None:
        # значение может содержать «=»: сплит строго по ПЕРВОМУ вхождению
        self.assertEqual(parse_set_arg("а=b=c"), ("а", "b=c"))

    def test_spaces_stripped(self) -> None:
        # края имени и значения обрезаются (эргономика CLI-аргумента)
        self.assertEqual(parse_set_arg("  Имя =  Знач  "), ("Имя", "Знач"))

    def test_empty_value_allowed(self) -> None:
        # «Имя=» — допустимо: SetValue("") = очистка поля
        self.assertEqual(parse_set_arg("Имя="), ("Имя", ""))

    def test_rejects_empty_string(self) -> None:
        # пустой аргумент — ошибка
        with self.assertRaises(ValueError):
            parse_set_arg("")

    def test_rejects_whitespace_string(self) -> None:
        # строка из пробелов — тоже пустой аргумент
        with self.assertRaises(ValueError):
            parse_set_arg("   ")

    def test_rejects_missing_equals(self) -> None:
        # нет «=» — не адресация поля, ошибка
        with self.assertRaises(ValueError):
            parse_set_arg("ИмяПоля")

    def test_rejects_empty_name(self) -> None:
        # «=Текст» без имени поля — ошибка (по чему искать?)
        with self.assertRaises(ValueError):
            parse_set_arg("=Текст")


class TestPasswordGuard(unittest.TestCase):
    """is_password_field: основы «парол»/«password» в любом регистре."""

    def test_cyrillic_lower(self) -> None:
        self.assertTrue(is_password_field("пароль"))

    def test_cyrillic_mixed_case(self) -> None:
        # «ПарОль» -> lower -> маркер найден: регистр не спасает
        self.assertTrue(is_password_field("ПарОль"))

    def test_cyrillic_substring(self) -> None:
        # основа «парол» ловит падежи: «пароля» НЕ содержит литеральное
        # «пароль», но типична для 1С («Подтверждение пароля») — гвард
        # обязан сработать
        self.assertTrue(is_password_field("Строка пароля"))
        self.assertTrue(is_password_field("Подтверждение пароля"))

    def test_latin_all_cases(self) -> None:
        # латиница: любой регистр
        for name in ("password", "PASSWORD", "PassWord"):
            self.assertTrue(is_password_field(name))

    def test_latin_substring(self) -> None:
        self.assertTrue(is_password_field("PASSWORD_FIELD"))

    def test_normal_fields_pass(self) -> None:
        # обычные поля конфигуратора пишутся
        for name in ("Имя базы", "Сервер", "Пользователь", ""):
            self.assertFalse(is_password_field(name))


class TestSummarize(unittest.TestCase):
    """summarize: stats из фейкового списка контролов (чистая функция)."""

    def test_mixed_controls(self) -> None:
        # 3 поля ввода (Edit/Combo/Document), 1 кнопка без паттерна,
        # 2 контрола с ValuePattern (кнопка-с-паттерном тоже считается)
        controls = [
            {"type": "EditControl", "value_available": True},
            {"type": "ButtonControl", "value_available": False},
            {"type": "ComboBoxControl", "value_available": True},
            {"type": "DocumentControl", "value_available": False},
        ]
        self.assertEqual(
            summarize(controls),
            {"total": 4, "edits": 3, "with_value_pattern": 2})

    def test_empty_list(self) -> None:
        # пустой скан — нулевые счётчики (и total=0, а не missing)
        self.assertEqual(
            summarize([]),
            {"total": 0, "edits": 0, "with_value_pattern": 0})

    def test_non_field_with_pattern_counts(self) -> None:
        # контрол НЕ из FIELD_TYPES, но с паттерном: в edits не идёт,
        # в with_value_pattern — идёт (инклюзивность скана по паттерну)
        self.assertEqual(
            summarize([{"type": "CustomControl", "value_available": True}]),
            {"total": 1, "edits": 0, "with_value_pattern": 1})


class TestMatchControl(unittest.TestCase):
    """match_control: поиск по ПОДСТРОКЕ имени, без учёта регистра."""

    def setUp(self) -> None:
        self.controls = [
            {"name": "Имя базы", "value_available": True},
            {"name": "Сервер", "value_available": False},
        ]

    def test_substring_match(self) -> None:
        # «баз» — подстрока «Имя базы»
        self.assertEqual(match_control(self.controls, "баз")["name"], "Имя базы")

    def test_case_insensitive(self) -> None:
        # регистр игнорируется («ИМЯ» матчит «Имя базы»)
        self.assertEqual(match_control(self.controls, "ИМЯ")["name"], "Имя базы")

    def test_first_match_wins(self) -> None:
        # при нескольких совпадениях берётся первый по порядку документа
        many = [{"name": "а-Сервер"}, {"name": "б-Сервер"}]
        self.assertEqual(match_control(many, "сервер")["name"], "а-Сервер")

    def test_no_match_returns_none(self) -> None:
        self.assertIsNone(match_control(self.controls, "неттакого"))
        self.assertIsNone(match_control([], "баз"))


class TestCheckTarget(unittest.TestCase):
    """check_target: гварды записи (общие для --set и --dry-write)."""

    def test_not_found(self) -> None:
        # name=None (совпадений нет) -> отказ, текст «не найдено»
        ok, err = check_target(None, False, "несуществующее")
        self.assertFalse(ok)
        self.assertIn("не найдено", err)

    def test_password_field_refused(self) -> None:
        # пароль-гвард: запись запрещена независимо от value_available
        ok, err = check_target("Пароль", True, "пар")
        self.assertFalse(ok)
        self.assertIn("пароль", err)
        self.assertIn("password", err)

    def test_no_value_pattern_refused(self) -> None:
        # поле найдено, но ValuePattern недоступен -> SetValue невозможен
        ok, err = check_target("Имя базы", False, "баз")
        self.assertFalse(ok)
        self.assertIn("ValuePattern", err)

    def test_ok_case(self) -> None:
        # валидная цель: ok=True, ошибка пустая
        self.assertEqual(check_target("Имя базы", True, "баз"), (True, ""))


class TestDryWriteResult(unittest.TestCase):
    """dry_write_result: показывает ЧТО было бы записано, без записи."""

    def test_would_write(self) -> None:
        # валидная цель: would_set=True, поле и текст названы
        target = {"name": "Имя базы", "value_available": True}
        res = dry_write_result(target, "баз", "Demo")
        self.assertTrue(res["would_set"])
        self.assertEqual(res["field"], "Имя базы")
        self.assertEqual(res["value"], "Demo")
        self.assertEqual(res["error"], "")

    def test_password_refused(self) -> None:
        # пароль-гвард и в dry-write: would_set=False + причина
        target = {"name": "Пароль", "value_available": True}
        res = dry_write_result(target, "Пароль", "hunter2")
        self.assertFalse(res["would_set"])
        self.assertIn("пароль", res["error"])

    def test_no_target(self) -> None:
        # target=None: would_set=False, поле = игла поиска
        res = dry_write_result(None, "НетТакого", "x")
        self.assertFalse(res["would_set"])
        self.assertEqual(res["field"], "НетТакого")
        self.assertIn("не найдено", res["error"])

    def test_no_pattern(self) -> None:
        # поле без ValuePattern: would_set=False, значение НЕ включается
        target = {"name": "Сервер", "value_available": False}
        res = dry_write_result(target, "Сервер", "localhost")
        self.assertFalse(res["would_set"])
        self.assertNotIn("value", res)
        self.assertIn("ValuePattern", res["error"])


class TestRunSetFakePattern(unittest.TestCase):
    """run_set с фейковым паттерном: SetValue и ошибки без UIA."""

    def test_set_value_called(self) -> None:
        # happy path: SetValue(text) вызывается ровно на текст из --set
        pat = _FakePattern()
        controls = [{"name": "Имя базы", "value_available": True}]
        res = run_set(controls, [pat], "баз", "Demo")
        self.assertEqual(res, {"set": True, "field": "Имя базы", "error": ""})
        self.assertEqual(pat.calls, ["Demo"])  # запись реально прошла в паттерн

    def test_set_value_error_reported(self) -> None:
        # COM-ошибка SetValue не роняет процесс: set=False + причина в error
        pat = _FakePattern(fail=True)
        controls = [{"name": "Имя базы", "value_available": True}]
        res = run_set(controls, [pat], "баз", "Demo")
        self.assertFalse(res["set"])
        self.assertIn("SetValue", res["error"])

    def test_no_match_refused(self) -> None:
        # совпадений нет: отказ «не найдено», паттерн не трогается
        pat = _FakePattern()
        res = run_set([], [], "абракадабра", "x")
        self.assertFalse(res["set"])
        self.assertIn("не найдено", res["error"])
        self.assertEqual(pat.calls, [])

    def test_password_never_written(self) -> None:
        # пароль-гвард в run_set: паттерн ЕСТЬ, но SetValue не вызывается
        pat = _FakePattern()
        controls = [{"name": "PASSWORD", "value_available": True}]
        res = run_set(controls, [pat], "pass", "hunter2")
        self.assertFalse(res["set"])
        self.assertIn("password", res["error"])
        self.assertEqual(pat.calls, [])  # ни одного вызова SetValue


class TestBuildReport(unittest.TestCase):
    """build_report: контракт JSON-отчёта скана."""

    def test_contract_keys(self) -> None:
        # все ключи контракта присутствуют, stats собран summarize'м
        controls = [{"type": "EditControl", "value_available": True,
                     "name": "Имя", "value": "x", "automation_id": "1",
                     "class_name": "V8x"}]
        rep = build_report(42, "Конфигуратор", controls, truncated=True)
        for key in ("pid", "window_title", "controls", "stats",
                    "truncated", "note"):
            self.assertIn(key, rep)
        self.assertEqual(rep["pid"], 42)
        self.assertEqual(rep["window_title"], "Конфигуратор")
        self.assertTrue(rep["truncated"])
        self.assertEqual(rep["stats"],
                         {"total": 1, "edits": 1, "with_value_pattern": 1})
        # note объясняет read-only по умолчанию и пароль-гвард
        self.assertIn("--set", rep["note"])
        self.assertIn("пароль", rep["note"])


class TestCliStructure(unittest.TestCase):
    """Структура CLI: ключи, обязательность, коды выхода."""

    def test_help_lists_keys(self) -> None:
        # --help перечисляет все ключи контракта
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        help_text = buf.getvalue()
        for key in ("--pid", "--out", "--set", "--dry-write"):
            self.assertIn(key, help_text)

    def test_missing_pid_system_exit_2(self) -> None:
        # --pid обязателен: argparse завершается с кодом 2
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                main([])
        self.assertEqual(cm.exception.code, 2)

    def test_dry_write_requires_set(self) -> None:
        # --dry-write без --set бессмыслен: честный rc=2, не скан
        self.assertEqual(main(["--pid", "0", "--dry-write"]), 2)

    def test_bad_set_spec_rc2(self) -> None:
        # кривой --set (без «=») ловится ДО скана: rc=2
        self.assertEqual(main(["--pid", "0", "--set", "ИмяБезРавно"]), 2)

    def test_contract_constants(self) -> None:
        # константы контракта: бюджеты обхода и типы «полей»
        self.assertEqual(MAX_DEPTH, 12)
        self.assertEqual(MAX_CONTROLS, 600)
        self.assertEqual(
            FIELD_TYPES,
            frozenset({"EditControl", "ComboBoxControl", "DocumentControl"}))
        # пароль-гвард закрыт с обеих сторон: основа кириллицы («парол»
        # ловит «пароль»/«пароля»/«пароли») + латиница
        self.assertIn("парол", PASSWORD_MARKERS)
        self.assertIn("password", PASSWORD_MARKERS)
        self.assertTrue(callable(probe))
        self.assertTrue(callable(main))


class TestEnvironmentRefusal(unittest.TestCase):
    """Честный отказ окружения: rc=2 без traceback, ленивые импорты."""

    def test_cli_pid0_rc2_no_out_file(self) -> None:
        # pid 0 (System Idle) не владеет окнами: на Windows — «нет видимых
        # окон» (или отказ uiautomation, если пакет не установлен); на Linux —
        # гвард платформы. Любой из честных путей = rc=2, --out НЕ создаётся.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "probe.json"
            rc = main(["--pid", "0", "--out", str(out)])
            self.assertEqual(rc, 2)
            self.assertFalse(out.exists())

    def test_probe_raises_on_linux(self) -> None:
        # не-Windows: probe -> RuntimeError с «Windows» и именем платформы
        with mock.patch("sys.platform", "linux"):
            with self.assertRaises(RuntimeError) as cm:
                probe(12345)
        self.assertIn("Windows", str(cm.exception))
        self.assertIn("linux", str(cm.exception))

    def test_lazy_imports_guarded(self) -> None:
        # на не-Windows гвард срабатывает ДО импорта uiautomation/win32 —
        # модуль живёт и тестируется без пакета (диф sys.modules пуст)
        imported_before = set(sys.modules)
        with mock.patch("sys.platform", "linux"):
            with self.assertRaises(RuntimeError):
                probe(1)
        self.assertNotIn("uiautomation", set(sys.modules) - imported_before)


if __name__ == "__main__":
    unittest.main()


class TestRunSetIndex(unittest.TestCase):
    """v1.17: запись по индексу — безымянные поля конфигуратора; гварды
    (границы/пароль/нет паттерна) — живого UIA не требуется."""

    class _FakePattern:
        def __init__(self):
            self.written = None

        def SetValue(self, text):
            self.written = text

    def _mk(self, name="", value_available=True, pat=True):
        return ({"name": name, "type": "EditControl", "value_available":
                 value_available, "value": ""},
                (self._FakePattern() if pat else None))

    def test_set_by_index_ok(self) -> None:
        from uia_value_probe import run_set_index
        c, pat = self._mk()
        res = run_set_index([c], [pat], 0, "mcp-probe")
        self.assertTrue(res["set"], res)
        self.assertEqual(res["error"], "")
        self.assertEqual(pat.written, "mcp-probe")  # дошло до SetValue

    def test_set_nameless_field_ok(self) -> None:
        # главный случай пробы: name пустой — индекс обходит матч по имени
        from uia_value_probe import run_set_index
        c, pat = self._mk(name="")
        res = run_set_index([c], [pat], 0, "текст")
        self.assertTrue(res["set"], res)
        self.assertEqual(res["field"], "#0")

    def test_index_out_of_bounds(self) -> None:
        from uia_value_probe import run_set_index
        res = run_set_index([], [], 3, "x")
        self.assertFalse(res["set"])
        self.assertIn("за границами", res["error"])

    def test_password_guard_applies(self) -> None:
        from uia_value_probe import run_set_index
        c, pat = self._mk(name="Подтверждение пароля")
        res = run_set_index([c], [pat], 0, "x")
        self.assertFalse(res["set"])
        self.assertIn("парольн", res["error"])

    def test_no_value_pattern(self) -> None:
        from uia_value_probe import run_set_index
        c, pat = self._mk(value_available=False, pat=None)
        res = run_set_index([c], [pat], 0, "x")
        self.assertFalse(res["set"])
        self.assertIn("ValuePattern", res["error"])

    def test_dead_pattern(self) -> None:
        from uia_value_probe import run_set_index
        c, pat = self._mk(pat=False)
        res = run_set_index([c], [pat], 0, "x")
        self.assertFalse(res["set"])
        self.assertIn("живой ValuePattern", res["error"])
