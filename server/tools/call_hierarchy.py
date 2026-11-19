# -*- coding: utf-8 -*-
"""Иерархия вызовов процедуры/функции по конфигурации (№16 продолжение).

Композиция двух готовых механизмов:
- определение: module_structure.parse по source_path (живой модуль) — точные
  line_start/line_end;
- вызовы: md_search_code.search по cf (быстрый записный сканер, без платформы) —
  все упоминания имени с «(» по всем модулям конфигурации.

Фильтрация вызовов: исключаются
- определения (строка, начинающаяся с Процедура/Функция + имя);
- КонецПроцедуры/КонецФункции;
- вызовы через точку (Объект.Имя — чужой метод);
- вхождения внутри слов (используется regex с границами).

Точность ограничения: поиск по cf находит литералы в текстах модулей —
в строках-литералах (Сообщить("Имя")) будут ложные срабатывания; для
исключения нужен полный лексер BSL (не входит в MVP). Поле note в ответе.
"""
from __future__ import annotations

import re


class HierarchyError(RuntimeError):
    pass


_DECL_RE = re.compile(r'^\s*(Процедура|Функция)\s+')
_END_RE = re.compile(r'^\s*(КонецПроцедуры|КонецФункции)\s*$')
_CALL_RE_TEMPLATE = (r'(?<![\w.])'      # не после слова/точки (член-доступ)
                      r'{name}'
                      r'(?![\w])')       # не часть большего имени


def call_pattern(name: str) -> re.Pattern:
    """Regex вызова: имя, за которым следует «(» с границами слова."""
    return re.compile(_CALL_RE_TEMPLATE.format(name=re.escape(name)) + r'\s*\(',
                      re.IGNORECASE)


def is_own_declaration(line: str, name: str) -> bool:
    """Строка — объявление ЭТОЙ процедуры/функции."""
    m = _DECL_RE.match(line)
    if not m:
        return False
    rest = line[m.end():]
    return bool(re.match(r'\s*' + re.escape(name) + r'\b', rest, re.IGNORECASE))


def group_calls(search_matches: list, name: str,
                max_per_object: int = 30) -> list:
    """Из ответа md_search_code (matches[].hits[]) собирает вызовы:
    отсекает строки-объявления и Конец*."""
    cp = call_pattern(name)
    out = []
    for m in search_matches:
        calls = []
        for h in m.get('hits', []):
            frag = h.get('fragment', '')
            for frag_line in frag.split('⏎'):
                line = frag_line.strip()
                if _END_RE.match(line):
                    continue
                if is_own_declaration(line, name):
                    continue
                if cp.search(line):
                    calls.append({'line': h['line'], 'fragment': line[:160]})
                    break
        if calls:
            out.append({'object': m['object'], 'record': m['record'],
                        'calls': calls[:max_per_object]})
    return out


def build(source_module_text: str | None, source_path: str | None,
          search_res: dict, name: str) -> dict:
    """Собирает итог иерархии: определение + вызовы по объектам."""
    definition = None
    if source_module_text:
        # локальный импорт — module_structure не тащим на верх
        from module_structure import parse
        for b in parse(source_module_text):
            if b['name'].lower() == name.lower():
                definition = {'path': source_path,
                              'kind': b['kind'],
                              'line_start': b['line_start'],
                              'line_end': b['line_end'],
                              'directives': b['directives'],
                              'export': b['export']}
                break
        if definition is None:
            raise HierarchyError(
                f"в {source_path} нет процедуры/функции «{name}»")
    grouped = group_calls(search_res.get('matches', []), name)
    total = sum(len(o['calls']) for o in grouped)
    return {'name': name, 'definition': definition,
            'objects_with_calls': grouped,
            'total_calls': total,
            'note': 'литералы строк могут давать ложные срабатывания (MVP '
                    'без лексера BSL); вызовы через точку исключены'}
