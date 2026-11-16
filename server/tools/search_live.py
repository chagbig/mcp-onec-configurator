# -*- coding: utf-8 -*-
"""Живой поиск по модулям конфигуратора без config_save (№15 дополнение).

md_search_code ищет по cf (сохранённая конфигурация). Здесь — обход живого
дерева через мост: md_tree (объекты) → module_get (with_text) для каждого
модуля → поиск подстроки. Медленнее cf-сканера (каждый модуль — отдельный
запрос к мосту), зато правки НЕ требуют config_save.

Ограничения MVP: только верхний уровень (объекты вида Справочник/Документ/
Обработка/Отчет/Регистры/ОбщийМодуль), только их главные модули
(МодульОбъекта/МодульМенеджера/Модуль). МОДУЛИ ФОРМ живьём НЕ сканируются:
module_get по формам мостом не подтверждён (проба 13.11.2026 — единственная
живая сессия висела в модальном диалоге, запросы не обслуживаются); для
форм используйте md_search_code по свежему cf (там модули форм ищутся —
quoted-строки сериализаций форм, см. md_search_code).
"""
from __future__ import annotations

import re

TOPLINE_KINDS = ['Справочники', 'Документы', 'Обработки', 'Отчеты',
                 'Общие модули', 'ОбщиеМодули']
MODULE_PROP = {'МодульОбъекта', 'МодульМенеджера', 'Модуль'}


def find_needle(text: str, needle: str, use_regex: bool,
                max_hits: int) -> list:
    """Хиты в одном тексте модуля: [(line, fragment)] с границами слов."""
    flags = re.IGNORECASE | re.MULTILINE
    pattern = (re.compile(needle, flags) if use_regex
               else re.compile(re.escape(needle), flags))
    hits = []
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        m = pattern.search(line)
        if m:
            hits.append({'line': i, 'fragment': line.strip()[:160]})
            if len(hits) >= max_hits:
                break
    return hits


def walk_objects(tk, session: int | None, container: int | None,
                 kinds: list[str] | None = None) -> list:
    """Плоский список верхнеуровневых объектов: [{path, kind}]."""
    out = []
    si = tk.session_info(session=session)
    for cont in si.get('containers', []):
        if container is not None and cont.get('index') != container:
            continue
        cnt = cont.get('index')
        kinds_res = tk.md_counts(session=session, container=cnt)
        for kind_row in kinds_res.get('kinds', []):
            kind = kind_row.get('kind')
            if kinds and kind not in kinds:
                continue
            # md_tree постранично
            offset = 0
            while True:
                r = tk.md_tree(session=session, container=cnt, kinds=[kind],
                               limit=200, offset=offset)
                for item in r.get('items', []):
                    if item.get('cls') in ('ОбщийМодуль',) or \
                            kind in ('Справочники', 'Документы', 'Обработки',
                                     'Отчеты'):
                        out.append({'path': f"{item.get('cls')}.{item.get('name')}",
                                    'kind': item.get('cls')})
                if r.get('hasMore'):
                    offset += len(r.get('items', []))
                else:
                    break
    return out


def search_live(tk, session: int | None, container: int | None,
                needle: str, use_regex: bool = False,
                max_hits_per_module: int = 5,
                limit_objects: int = 500) -> dict:
    """Обход живых модулей и поиск подстроки. needle — подстрока или regex."""
    import re as _re
    if use_regex:
        _re.compile(needle, _re.IGNORECASE)
    objs = walk_objects(tk, session, container)
    matches = []
    scanned = 0
    for obj in objs[:limit_objects]:
        for prop in ('МодульОбъекта', 'МодульМенеджера', 'Модуль'):
            try:
                mod = tk.module_get(session=session, container=container,
                                    path=obj['path'], prop=prop, with_text=True)
            except Exception:
                continue
            if not mod.get('text'):
                continue
            scanned += 1
            hits = find_needle(mod['text'], needle, use_regex,
                               max_hits_per_module)
            if hits:
                matches.append({'object': obj['path'], 'prop': prop,
                                'hits': hits})
    return {'needle': needle, 'regex': use_regex,
            'objects_seen': len(objs), 'modules_scanned': scanned,
            'matches': matches,
            'total_hits': sum(len(m['hits']) for m in matches),
            'note': 'живой поиск по несохранённой конфигурации; модули '
                    'верхнего уровня; МОДУЛИ ФОРМ живьём не сканируются '
                    '(module_get по формам не подтверждён) — md_search_code '
                    'по свежему cf (модули форм там включены)'}
