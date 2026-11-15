"""Тесты парсера структуры модуля 1С (server/tools/module_structure.py).

IDEAS №16: номера строк процедур/функций для точечных правок bridge module_patch.
Синтетические BSL-тексты (константы ниже, utf-8), платформа не нужна.
Прогон (из корня проекта):
  python -m unittest discover -s server/tests -p "test_module_structure.py"
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from module_structure import find_call_sites, parse  # noqa: E402

# (а) две процедуры: первая Экспорт без директивы, вторая с &НаСервере
MOD_TWO_PROC = (
    "// Демо-модуль: две процедуры\n"
    "Перем Кэш Экспорт;\n"
    "\n"
    "Процедура ЗаполнитьКэш(Данные) Экспорт\n"
    "\tКэш = Данные;\n"
    "КонецПроцедуры\n"
    "\n"
    "&НаСервере\n"
    "Процедура Служебная(Знач Флаг = Ложь)\n"
    "\tЗаполнитьКэш(\"\");\n"
    "КонецПроцедуры\n"
)

# (б) слово «Процедура» в комментариях и в литерале — блок не создаёт
MOD_COMMENT = (
    "// Описание: Процедура ЗагрузитьДанные() читает файл выгрузки\n"
    "// см. также: Функция Вспомогательная(х) упоминается в задачнике\n"
    "Процедура ЗагрузитьДанные(Идентификатор)\n"
    "\tТекст = \"Процедура не найдена\";\n"
    "\tСообщить(Текст);\n"
    "КонецПроцедуры\n"
)

# (в) функция + вызовы: объявление и доступ через точку — не вызовы
MOD_CALLS = (
    "Функция ВычислитьСумму(А, Б) Экспорт\n"
    "\tВозврат А + Б;\n"
    "КонецФункции\n"
    "\n"
    "Процедура Проверить()\n"
    "\tСумма = ВычислитьСумму(1, 2);\n"
    "\tСумма = ВычислитьСумму (Сумма, 3);   // пробел перед скобкой — тоже вызов\n"
    "\tЧужое = КакойТоОбъект.ВычислитьСумму(5);  // доступ через точку — не наш вызов\n"
    "КонецПроцедуры\n"
)

# (д) незакрытый блок
MOD_UNCLOSED = (
    "&НаКлиенте\n"
    "Процедура Незакрытая(П)\n"
    "\tП = П + 1;\n"
)

# «Функция» без «(» и «Процедуры...» без границы слова — не объявления
MOD_NO_PAREN = (
    "Функция БезСкобок      // нет открывающей скобки — не объявление\n"
    "Функция СоСкобкой()\n"
    "\tВозврат 1;\n"
    "КонецФункции\n"
    "\n"
    "Процедура Держатель()\n"
    "\tПроцедурыНабора(Ключ);      // «Процедура» без границы слова\n"
    "КонецПроцедуры\n"
)

# Токен без имени («Процедура (х)») — не блок
MOD_NO_NAME = (
    "Процедура (безИмени)      // не создаёт блока: нет имени\n"
    "Процедура СИменем()\n"
    "КонецПроцедуры\n"
)

# Строковый литерал в значении параметра по умолчанию, содержащий «)»
MOD_PARAM_STR = (
    "Процедура Т(П = \"скобка)\") Экспорт\n"
    "КонецПроцедуры\n"
)

# Директива привязывается через комментарии и пустые строки
MOD_DIRECTIVE_GAPS = (
    "&НаСервере\n"
    "// комментарий между директивой и объявлением\n"
    "Процедура А(П)\n"
    "КонецПроцедуры\n"
    "\n"
    "&НаКлиенте\n"
    "\n"
    "Процедура Б()\n"
    "КонецПроцедуры\n"
)


class TestModuleStructure(unittest.TestCase):

    def test_two_procedures(self):
        """(а) line_start/line_end/directives/export/params двух процедур."""
        blocks = parse(MOD_TWO_PROC)
        self.assertEqual(len(blocks), 2)
        b1, b2 = blocks
        self.assertEqual(b1["name"], "ЗаполнитьКэш")
        self.assertEqual(b1["kind"], "Процедура")
        self.assertEqual(b1["line_start"], 4)
        self.assertEqual(b1["line_end"], 6)
        self.assertEqual(b1["directives"], [])
        self.assertTrue(b1["export"])
        self.assertEqual(b1["params"], "Данные")
        self.assertEqual(b2["name"], "Служебная")
        self.assertEqual(b2["kind"], "Процедура")
        self.assertEqual(b2["line_start"], 9)
        self.assertEqual(b2["line_end"], 11)
        self.assertEqual(b2["directives"], ["&НаСервере"])
        self.assertFalse(b2["export"])
        self.assertEqual(b2["params"], "Знач Флаг = Ложь")

    def test_comment_with_word_procedure(self):
        """(б) «Процедура» в комментарии/литерале не создаёт блок."""
        blocks = parse(MOD_COMMENT)
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertEqual(b["name"], "ЗагрузитьДанные")
        self.assertEqual(b["line_start"], 3)
        self.assertEqual(b["line_end"], 6)
        self.assertEqual(b["directives"], [])
        self.assertEqual(b["params"], "Идентификатор")

    def test_function_and_call_sites(self):
        """(в) find_call_sites находит вызовы и не считает объявление вызовом."""
        blocks = parse(MOD_CALLS)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["name"], "ВычислитьСумму")
        self.assertEqual(blocks[0]["kind"], "Функция")
        self.assertEqual(blocks[0]["line_start"], 1)
        self.assertEqual(blocks[0]["line_end"], 3)
        self.assertTrue(blocks[0]["export"])
        self.assertEqual(blocks[0]["params"], "А, Б")
        self.assertEqual(blocks[1]["name"], "Проверить")
        self.assertEqual(blocks[1]["line_start"], 5)
        self.assertEqual(blocks[1]["line_end"], 9)

        sites = find_call_sites(MOD_CALLS, "ВычислитьСумму")
        self.assertEqual([s["line"] for s in sites], [6, 7])  # 1 — объявление, 8 — через точку
        self.assertEqual(sites[0]["fragment"], "Сумма = ВычислитьСумму(1, 2);")
        # регистр идентификаторов в BSL не различается
        self.assertEqual([s["line"] for s in find_call_sites(MOD_CALLS, "вычислитьсумму")], [6, 7])

    def test_lf_only_eol(self):
        """(г) LF-only EOL разбирается так же, как CRLF; номера строк те же."""
        blocks = parse(MOD_TWO_PROC)  # константы уже LF-only
        self.assertEqual([b["line_start"] for b in blocks], [4, 9])
        self.assertEqual([b["line_end"] for b in blocks], [6, 11])
        self.assertEqual(parse(MOD_TWO_PROC.replace("\n", "\r\n")), blocks)

    def test_unclosed_block(self):
        """(д) незакрытый блок — line_end=None, директивы и параметры на месте."""
        blocks = parse(MOD_UNCLOSED)
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertEqual(b["name"], "Незакрытая")
        self.assertEqual(b["line_start"], 2)
        self.assertIsNone(b["line_end"])
        self.assertEqual(b["directives"], ["&НаКлиенте"])
        self.assertEqual(b["params"], "П")
        self.assertFalse(b["export"])

    def test_declaration_without_paren_skipped(self):
        """«Функция» без «(» и «Процедуры...» без границы слова — не блоки."""
        blocks = parse(MOD_NO_PAREN)
        self.assertEqual([(b["name"], b["kind"], b["line_start"], b["line_end"])
                          for b in blocks],
                         [("СоСкобкой", "Функция", 2, 4), ("Держатель", "Процедура", 6, 8)])

    def test_declaration_without_name_skipped(self):
        """Токен без имени («Процедура (х)») — не блок."""
        blocks = parse(MOD_NO_NAME)
        self.assertEqual([(b["name"], b["line_start"], b["line_end"]) for b in blocks],
                         [("СИменем", 2, 3)])

    def test_params_string_default_with_paren(self):
        """Литерал с «)» в значении по умолчанию не рвёт список параметров."""
        b = parse(MOD_PARAM_STR)[0]
        self.assertEqual(b["params"], "П = \"скобка)\"")
        self.assertTrue(b["export"])

    def test_directives_with_gaps(self):
        """Директива привязывается через комментарии и пустые строки."""
        blocks = parse(MOD_DIRECTIVE_GAPS)
        self.assertEqual([b["directives"] for b in blocks], [["&НаСервере"], ["&НаКлиенте"]])
        self.assertEqual([b["line_start"] for b in blocks], [3, 8])

    def test_call_sites_invalid_name(self):
        """Пустое/не идентификатор имя и неизвестное имя — без результатов."""
        self.assertEqual(find_call_sites(MOD_TWO_PROC, ""), [])
        self.assertEqual(find_call_sites(MOD_TWO_PROC, "1плохо"), [])
        self.assertEqual(find_call_sites(MOD_TWO_PROC, "НетТакой"), [])


if __name__ == "__main__":
    unittest.main()
