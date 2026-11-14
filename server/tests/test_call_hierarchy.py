# -*- coding: utf-8 -*-
"""Тесты call_hierarchy (№16 завершение): группировка вызовов, фильтры, build."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


class TestCallPattern(unittest.TestCase):
    def test_matches_call_not_definition(self) -> None:
        from call_hierarchy import call_pattern, is_own_declaration
        cp = call_pattern('РасчетСуммы')
        self.assertTrue(cp.search('х = РасчетСуммы(1, 2);'))
        self.assertTrue(cp.search('РасчетСуммы();'))
        # объявление тоже матчится паттерном — отсекается group_calls через
        # is_own_declaration (паттерн имени не различает контекст)
        self.assertTrue(cp.search('Процедура РасчетСуммы(А)'))
        self.assertFalse(cp.search('РасчетСуммыНовая();'))
        self.assertFalse(cp.search('Объект.РасчетСуммы();'))
        # имя внутри строкового литерала паттерн НЕ отличает — это ложное
        # срабатывание уровня MVP (заявлено в note ответа), лексер — вне скоупа
        self.assertTrue(is_own_declaration('Процедура РасчетСуммы(А) Экспорт', 'РасчетСуммы'))
        self.assertFalse(is_own_declaration('х = РасчетСуммы(1);', 'РасчетСуммы'))


class TestGroupCalls(unittest.TestCase):
    def test_groups_and_filters(self) -> None:
        from call_hierarchy import group_calls
        matches = [
            {'object': 'Справочник.Товары', 'record': 'xxx.0', 'hits': [
                {'line': 3, 'fragment': 'х = РасчетСуммы(1); ⏎ КонецФункции'},
                {'line': 7, 'fragment': 'Процедура РасчетСуммы(А) — определение'},
            ]},
        ]
        out = group_calls(matches, 'РасчетСуммы')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['object'], 'Справочник.Товары')
        self.assertEqual(len(out[0]['calls']), 1)
        self.assertEqual(out[0]['calls'][0]['line'], 3)


class TestBuild(unittest.TestCase):
    def test_build_with_definition(self) -> None:
        from call_hierarchy import build
        module = ('Функция РасчетСуммы(А, Б) Экспорт\n'
                  '\tВозврат А + Б;\n'
                  'КонецФункции\n')
        search_res = {'matches': [
            {'object': 'Справочник.Товары', 'record': 'x.0', 'hits': [
                {'line': 10, 'fragment': 'х = РасчетСуммы(1, 2);'}]},
        ]}
        res = build(module, 'Справочник.Товары', search_res, 'РасчетСуммы')
        self.assertEqual(res['definition']['line_start'], 1)
        self.assertEqual(res['definition']['line_end'], 3)
        self.assertTrue(res['definition']['export'])
        self.assertEqual(res['total_calls'], 1)

    def test_build_definition_missing_raises(self) -> None:
        from call_hierarchy import build, HierarchyError
        with self.assertRaises(HierarchyError):
            build('Сообщить(1);\n', 'Справочник.Х', {'matches': []}, 'НетТакой')


if __name__ == '__main__':
    unittest.main()
