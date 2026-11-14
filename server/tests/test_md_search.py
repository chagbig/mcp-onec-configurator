"""Тесты md_search_code: поиск по текстам модулей в cf (без платформы).

Прогон: python -m unittest discover -s server/tests -p "test_md_search.py"
(из корня проекта).

Чистые тесты search_record — на СИНТЕТИЧЕСКИХ байтах (маркер text utf-16le,
заголовки страниц «\\r\\n<size> <page> <mask> \\r\\n», строки модуля через
«\\r\\r\\n»), без зависимости от cf-файла. Живой тест — skipUnless на probe-cf
(может исчезнуть при чистке backups, тогда тест пропускается).
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from md_search_code import search_record, search  # noqa: E402
from md_search_code import _form_module_texts, _text_pages  # noqa: E402

BOM = b"\xef\xbb\xbf"
TEXT_MARKER = b"t\x00e\x00x\x00t\x00\x00\x00\x00\x00"   # «text» utf-16le + 2 NUL
INFO_MARKER = b"i\x00n\x00f\x00o\x00\x00\x00\x00\x00"
CONTAINER_HEAD = b"\xff\xff\xff\x7f\x00\x02\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00"

MODULE_LINES = [
    "Процедура ПриСозданииЭлемента(Отказ, СтандартнаяОбработка)",
    '\tСообщить("Создание элемента: " + ЭтотОбъект.Код);',
    "КонецПроцедуры",
    "",
    "Процедура ПередЗаписью(Отказ)",
    '\tСообщить("Перед записью");',
    "КонецПроцедуры",
]
NEEDLE_CREATE = 'Сообщить("Создание элемента'

PROBE_CF = r"Y:\Snegopat-mcp\backups\mcp-20261110-search-probe.cf"
PROBE_RECORD = "4c050b66-4d15-42fe-97fc-1b3cf769e335.0"
PROBE_FORM_RECORD = "110ae474-9f44-4b82-9b25-677f609b4323.0"

# модуль формы: quoted-строка в сериализации формы; кавычки в содержимом
# УДВОЕНЫ («""»), разделители строк — «\r\n» (проба 13.11.2026, demossl)
FORM_LINES = [
    "#Область ОбработчикиСобытийФормы",
    "",
    "&НаСервере",
    "Процедура ПриСозданииНаСервере(Отказ, СтандартнаяОбработка)",
    '\tЕсли Не Параметры.Свойство(""ПутьКФорме"") Тогда',
    "\t\tОтказ = Истина;",
    "\tКонецЕсли;",
    "КонецПроцедуры",
    "",
    "#КонецОбласти",
]
FORM_TEXT = "\r\n".join(FORM_LINES)


def _form_record(module_text: str, title: str = "Ценные бумаги") -> bytes:
    """Синтетический blob сериализации ФОРМЫ: заголовок с {"ru","title"} и
    quoted-строка модуля (начинается со скелета «#Область …Формы»).
    Текст модуля хранится как в cf — с УДВОЕННЫМИ кавычками."""
    payload = ('{"ru","' + title + '"},\r\n{50,0},1},"'
               + module_text + '",\r\n{4,1,')
    return CONTAINER_HEAD + payload.encode("utf-8")


def _block(size: int, page: int, region: bytes) -> bytes:
    """Блок вложенного контейнера: заголовок + регион ровно page байт."""
    assert len(region) == page, "регион блока должен занимать ровно page байт"
    return ("\r\n%08x %08x %08x \r\n" % (size, page, 0x7FFFFFFF)).encode("ascii") + region


def _num_block() -> bytes:
    return _block(0x18, 0x18, b"\x47\x00\x00\x00" + b"\x00" * 20)


def _name_block(marker: bytes) -> bytes:
    """Блок-«имя» (page=32): 20 нулей + «text»/«info» utf-16le + 4 нуля."""
    return _block(0x20, 0x20, b"\x00" * 20 + marker[:8] + b"\x00\x00\x00\x00")


def _module_bytes(lines: list[str], sep: str = "\r\r\n") -> bytes:
    return (sep.join(lines) + sep).encode("utf-8")


def _record(module_bytes: bytes | None, *, sep: str = "\r\r\n",
            text_stale: bytes = b"", info_stale: bytes = b"",
            module_sep: str | None = None) -> bytes:
    """Синтетический .0-blob: числовой блок, info-имя, info-страница,
    text-имя, страница текста. Регионы длиннее содержимого набиты «устаревшим»
    мусором (как в реальном cf). module_bytes=None — запись без страниц text."""
    parts = [CONTAINER_HEAD, _num_block(), _name_block(INFO_MARKER)]
    info_region = BOM + b'{3,1,0,"",0}'
    info_page = 0x40
    parts.append(_block(0x0F, info_page, info_region.ljust(info_page, b"\x2e")[:0x0F]
                        + info_stale.ljust(info_page - 0x0F, b"\x2e")))
    if module_bytes is not None:
        parts.append(_name_block(TEXT_MARKER))
        size = len(module_bytes)
        page = size + len(text_stale)
        parts.append(_block(size, page, module_bytes + text_stale))
    return b"".join(parts)


class TestSearchRecordSynthetic(unittest.TestCase):
    """Чистые тесты search_record на синтетических байтах."""

    def _rec(self, sep: str = "\r\r\n", **kw) -> bytes:
        return _record(_module_bytes(MODULE_LINES, sep), sep=sep, **kw)

    def test_exact_substring_line_number_crlfcr(self):
        """(а) точная подстрока — правильный номер строки (разделитель \\r\\r\\n)."""
        hits = search_record(self._rec(), NEEDLE_CREATE, False, 20)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["line"], 2)
        self.assertEqual(hits[0]["col"], 1)
        self.assertIn("Создание элемента", hits[0]["fragment"])

    def test_line_numbers_lf_quirk(self):
        """(а) тот же номер строки при разделителе \\r\\n (вендорский модуль)."""
        hits = search_record(self._rec(sep="\r\n"), NEEDLE_CREATE, False, 20)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["line"], 2)
        hits = search_record(self._rec(sep="\r\n"), "КонецПроцедуры", False, 20)
        self.assertEqual([h["line"] for h in hits], [3, 7])

    def test_hit_outside_text_page_rejected(self):
        """(б) попадание вне страницы text отбрасывается (info-страница,
        устаревший хвост страницы текста за границей size, запись без text)."""
        outside = "ПОДСТРОКА_ВНЕ_СТРАНИЦЫ".encode("utf-8")
        # устаревший хвост СТРАНИЦЫ ТЕКСТА за границей size (кейс ВидыЦенныхБумаг)
        rec = _record(_module_bytes(MODULE_LINES), text_stale=b"\r\r\n" + outside + b"\r\r\n")
        self.assertEqual(search_record(rec, "ПОДСТРОКА_ВНЕ_СТРАНИЦЫ", False, 20), [])
        # обычный запрос при этом находится
        self.assertEqual(len(search_record(rec, NEEDLE_CREATE, False, 20)), 1)
        # устаревший хвост INFO-страницы
        rec = _record(_module_bytes(MODULE_LINES), info_stale=b"||" + outside)
        self.assertEqual(search_record(rec, "ПОДСТРОКА_ВНЕ_СТРАНИЦЫ", False, 20), [])
        # запись вообще без страниц text (плоская сериализация)
        plain = CONTAINER_HEAD + (BOM + ('{1,0,uuid},"Имя",\r\n'
                                         "ПОДСТРОКА_ВНЕ_СТРАНИЦЫ\r\n").encode("utf-8"))
        self.assertEqual(search_record(plain, "ПОДСТРОКА_ВНЕ_СТРАНИЦЫ", False, 20), [])

    def test_case_insensitive(self):
        """(в) поиск без учёта регистра (кириллица и латиница)."""
        hits = search_record(self._rec(), 'сообщить("СОЗДАНИЕ элемента', False, 20)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["line"], 2)
        hits = search_record(self._rec(), "конецпроцедуры", False, 20)
        self.assertEqual([h["line"] for h in hits], [3, 7])

    def test_regex_mode(self):
        """(г) regex-режим: несколько попаданий с верными строками."""
        hits = search_record(self._rec(), r"Сообщить\(", True, 20)
        self.assertEqual([h["line"] for h in hits], [2, 6])
        hits = search_record(self._rec(), r"^\tСообщить", True, 20)
        self.assertEqual([h["line"] for h in hits], [2, 6])
        # в regex-режиме спецсимволы не экранируются: точка — любой символ
        hits = search_record(self._rec(), r"ЭтотОбъект.Код", True, 20)
        self.assertEqual([h["line"] for h in hits], [2])

    def test_max_hits_cap(self):
        rec = _record(_module_bytes(MODULE_LINES))
        hits = search_record(rec, r"Сообщить\(", True, 1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["line"], 2)

    def test_multiple_text_pages(self):
        """Два блока text в одной записи: каждая страница нумерует строки с 1."""
        m1 = _module_bytes(MODULE_LINES)
        m2 = _module_bytes(["Функция Вторая()", "\tСообщить(\"Вторая\");",
                            "КонецФункции"])
        parts = [CONTAINER_HEAD, _num_block(), _name_block(INFO_MARKER),
                 _block(0x0F, 0x40, (BOM + b'{3,1,0,"",0}').ljust(0x40, b"\x2e")),
                 _name_block(TEXT_MARKER), _block(len(m1), len(m1), m1),
                 _name_block(TEXT_MARKER), _block(len(m2), len(m2), m2)]
        rec = b"".join(parts)
        hits = search_record(rec, "Сообщить(", False, 20)
        # страница 1: строки 2 и 6; страница 2: строка 2 (нумерация с 1)
        self.assertEqual([h["line"] for h in hits], [2, 6, 2])
        hits = search_record(rec, "Вторая", False, 20)
        self.assertEqual([h["line"] for h in hits], [1, 2])

    def test_no_hits_without_text_marker(self):
        rec = _record(None)
        self.assertEqual(search_record(rec, "Сообщить", False, 20), [])
        self.assertEqual(search_record(b"", "Сообщить", False, 20), [])


class TestFormModuleRecord(unittest.TestCase):
    """Модули форм в cf — quoted-строки сериализаций форм (проба 13.11.2026:
    probe-cf и demossl-A; страницы text у форм отсутствуют)."""

    def test_form_record_has_no_text_pages(self):
        rec = _form_record(FORM_TEXT)
        self.assertEqual(_text_pages(rec), [])  # у форм страниц text НЕТ
        self.assertEqual(len(_form_module_texts(rec)), 1)

    def test_form_module_hit_unescaped(self):
        """Хит в модуле формы: src="form", кавычки разэкранированы,
        строка по «\\r\\n»."""
        hits = search_record(_form_record(FORM_TEXT), "ПутьКФорме", False, 20)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["src"], "form")
        self.assertEqual(hits[0]["line"], 5)
        self.assertIn('Свойство("ПутьКФорме")', hits[0]["fragment"])

    def test_form_needle_with_quotes(self):
        """needle с кавычками находит по РАЗЭКРАНИРОВАННОМУ тексту
        (в cf хранится «Свойство(""ПутьКФорме"")»)."""
        hits = search_record(_form_record(FORM_TEXT),
                             'Свойство("ПутьКФорме")', False, 20)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["line"], 5)

    def test_doubled_quote_marker_is_not_string_start(self):
        """«""#Область…» внутри чужой строки — не начало модуля, хитов нет."""
        blob = CONTAINER_HEAD + ('{"a",""' + "#Область ОбработчикиСобытийФормы"
                                 + "\r\nхвост\"}").encode("utf-8")
        self.assertEqual(search_record(blob, "#Область", False, 20), [])

    def test_page_and_form_in_one_record(self):
        """Запись и со страницей text, и с quoted-модулем формы: оба хита,
        src различает, нумерация независимая."""
        rec = _record(_module_bytes(MODULE_LINES)) + _form_record(FORM_TEXT)[len(CONTAINER_HEAD):]
        hits = search_record(rec, "ПутьКФорме", False, 20)
        self.assertEqual([h["src"] for h in hits], ["form"])
        hits2 = search_record(rec, NEEDLE_CREATE, False, 20)
        self.assertEqual([h["src"] for h in hits2], ["page"])
        self.assertEqual(hits2[0]["line"], 2)


@unittest.skipUnless(os.path.isfile(PROBE_CF), "probe-cf отсутствует (чистка backups)")
class TestLiveProbeCf(unittest.TestCase):
    """Живой прогон на probe-cf (если файл ещё существует)."""

    def test_create_element_hit(self):
        """«Сообщить("Создание элемента» → Эмитенты, запись 4c050b66…0, строка 2."""
        res = search(PROBE_CF, NEEDLE_CREATE, False, limit=10)
        self.assertEqual(res["total_hits"], 1)
        m = res["matches"][0]
        self.assertEqual(m["record"], PROBE_RECORD)
        self.assertEqual(m["object"], "Эмитенты")
        self.assertTrue(m["object_known"])
        self.assertEqual(m["hits"][0]["line"], 2)

    def test_vendor_module_hit(self):
        """«Сумма расчёта» → вендорский модуль РасчётСКлиентом (имя из карты)."""
        res = search(PROBE_CF, "Сумма расчёта", False, limit=10)
        self.assertGreaterEqual(res["total_hits"], 1)
        m = res["matches"][0]
        self.assertEqual(m["object"], "РасчётСКлиентом")
        self.assertEqual(m["hits"][0]["line"], 9)

    def test_regex_several_objects(self):
        """--regex 'Сообщить\\(' — попадания в нескольких объектах."""
        res = search(PROBE_CF, r"Сообщить\(", True, limit=50)
        objects = {m["object"] for m in res["matches"]}
        self.assertGreaterEqual(len(objects), 3)
        self.assertIn("Эмитенты", objects)

    def test_no_false_hits_in_emptied_modules(self):
        """Модули, опустевшие в сохранённой конфигурации (size=3), не дают
        ложных попаданий по устаревшему тексту в хвостах регионов."""
        res = search(PROBE_CF, "Сумма расчёта", False, limit=50)
        records = {m["record"] for m in res["matches"]}
        self.assertNotIn("7e9db411-3015-4ca5-b890-bd959bb93fed.0", records)
        self.assertNotIn("e0161451-532a-4240-8735-6b062cbb438e.0", records)


@unittest.skipUnless(os.path.isfile(PROBE_CF), "probe-cf отсутствует (чистка backups)")
class TestLiveProbeCfForms(unittest.TestCase):
    """Формы на probe-cf: 19 сериализаций форм, модули — скелеты (пустые),
    но quoted-строки находятся и атрибуция форм работает."""

    def test_form_skeleton_hit_and_attribution(self):
        res = search(PROBE_CF, "#Область ОбработчикиСобытийФормы", False,
                     limit=100)
        self.assertGreaterEqual(res["total_hits"], 1)
        for m in res["matches"]:
            self.assertTrue(m.get("form_record"), m["record"])
        by_rec = {m["record"]: m for m in res["matches"]}
        m = by_rec[PROBE_FORM_RECORD]
        self.assertEqual(m["object"], "ФормаЭлемента")
        self.assertTrue(m["object_known"])
        self.assertEqual(m["form_title"], "Ценные бумаги")
        self.assertEqual(m["hits"][0]["src"], "form")
        self.assertEqual(m["hits"][0]["line"], 1)

    def test_object_module_hit_is_page_src(self):
        """Обычный модуль объекта: src="page", form_record не проставляется."""
        res = search(PROBE_CF, NEEDLE_CREATE, False, limit=10)
        self.assertEqual(res["total_hits"], 1)
        m = res["matches"][0]
        self.assertEqual(m["object"], "Эмитенты")
        self.assertEqual(m["hits"][0]["src"], "page")
        self.assertNotIn("form_record", m)


if __name__ == "__main__":
    unittest.main()
