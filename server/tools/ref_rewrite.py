# -*- coding: utf-8 -*-
"""Переписывание ссылок при каскадном переименовании (IDEAS №17).

Обобщение механики rename_pairs этапа 19 (build_ext) на ЛЮБЫЕ объекты:
- англ квалификаторы с суффиксами (Document.X, DocumentRef.X, DocumentObject.X…);
- рус менеджеры коллекций (Документы.X — точка доступа в коде);
- рус типы (ДокументСсылка.X, СправочникОбъект.X…).
ОбщийМодуль — голое имя (вызов модуля в коде = имя), включается флагом
allow_bare (опасно при коротком имени, совпадающем с переменной — гвард в
md_rename: имя >= 5 символов).

targets_from_dump: модульные файлы XML-выгрузки, содержащие литералы старого
имени → (живой path, prop модуля). Формы помечаются отдельно (их контур —
form_get_xml/form_set_xml, не module_set).
"""
from __future__ import annotations

import re
from pathlib import Path

# вид RU: (EN, суффиксы EN, рус менеджер коллекции, рус типы)
KINDS = {
    "Документ": ("Document", ("", "Ref", "Object", "Selection", "List", "Manager"),
                 "Документы",
                 ("ДокументСсылка", "ДокументОбъект", "ДокументСписок",
                  "ДокументВыборка", "ДокументМенеджер")),
    "Справочник": ("Catalog", ("", "Ref", "Object", "Selection", "List", "Manager"),
                   "Справочники",
                   ("СправочникСсылка", "СправочникОбъект", "СправочникСписок",
                    "СправочникВыборка", "СправочникМенеджер")),
    "Обработка": ("DataProcessor", ("", "Object", "Manager"),
                  "Обработки", ("ОбработкаОбъект", "ОбработкаМенеджер")),
    "Отчет": ("Report", ("", "Object", "Manager"),
              "Отчеты", ("ОтчетОбъект", "ОтчетМенеджер")),
    "Перечисление": ("Enum", ("", "Ref", "List", "Manager"),
                     "Перечисления", ("ПеречислениеСсылка", "ПеречислениеМенеджер")),
    "РегистрСведений": ("InformationRegister", ("", "RecordSet", "Selection", "Manager"),
                        "РегистрыСведений", ("РегистрСведенийМенеджер",)),
    "РегистрНакопления": ("AccumulationRegister", ("", "RecordSet", "Selection", "Manager"),
                          "РегистрыНакопления", ("РегистрНакопленияМенеджер",)),
    "ПланВидовХарактеристик": ("ChartOfCharacteristicTypes",
                               ("", "Ref", "Object", "Selection", "List", "Manager"),
                               "ПланыВидовХарактеристик",
                               ("ПланВидовХарактеристикСсылка", "ПланВидовХарактеристикОбъект")),
    "ОбщаяФорма": ("CommonForm", ("",), None, ()),
    "ОбщийМодуль": ("CommonModule", ("",), None, ()),
}

# каталог выгрузки → вид RU
DIR2KIND = {"Documents": "Документ", "Catalogs": "Справочник", "DataProcessors": "Обработка",
            "Reports": "Отчет", "Enums": "Перечисление",
            "InformationRegisters": "РегистрСведений",
            "AccumulationRegisters": "РегистрНакопления",
            "ChartsOfCharacteristicTypes": "ПланВидовХарактеристик",
            "CommonForms": "ОбщаяФорма", "CommonModules": "ОбщийМодуль"}

# файл модуля в выгрузке → имя свойства (module_get/module_set)
MODULE_FILE2PROP = {
    "ObjectModule.bsl": "МодульОбъекта",
    "ManagerModule.bsl": "МодульМенеджера",
    "Module.bsl": "Модуль",              # CommonModules
    "RecordSetModule.bsl": "МодульНабораЗаписей",
    "CommandModule.bsl": "МодульКоманды",
    "SessionModule.bsl": "МодульСеанса",
}


def rewrite_refs(text: str, kind_ru: str, old: str, new: str,
                 allow_bare: bool = False) -> str:
    """Литералы ссылок на объект: EN-квалификаторы+суффиксы, рус менеджер,
    рус типы; ОбщийМодуль — голое \\bимя\\b при allow_bare."""
    spec = KINDS.get(kind_ru)
    if not spec:
        raise ValueError("вид не поддержан: " + kind_ru)
    kind_en, suffixes, plural, ru_types = spec
    old_e = re.escape(old)
    new_e = new.replace("\\", "\\\\")

    def sub_keep_head(m):
        return m.group(1) + "." + new_e

    if kind_en:
        alts = "|".join(re.escape(kind_en + s) for s in suffixes)
        # рус вид единственным числом — имя таблицы в ЗАПРОСАХ («ИЗ Документ.X»)
        text = re.sub(rf"\b({alts}|{re.escape(kind_ru)})\.{old_e}\b", sub_keep_head, text)
    if plural:
        text = re.sub(rf"\b({re.escape(plural)})\.{old_e}\b", sub_keep_head, text)
    for rt in ru_types:
        text = re.sub(rf"\b({re.escape(rt)})\.{old_e}\b", sub_keep_head, text)
    if allow_bare and kind_ru == "ОбщийМодуль":
        # голый вызов: не слово и не точка СЛЕВА (не член-доступ), справа не
        # буквенно-цифровое (точка разрешена — «Имя.Функция()»)
        text = re.sub(rf"(?<![\w.]){old_e}(?![\w])", new_e, text)
    return text


def module_targets_from_dump(dump_dir: str, old_name: str) -> tuple:
    """Модульные файлы выгрузки с литералом старого имени →
    (targets: [{rel_path, path, prop}], forms: [rel_path]).
    path — живой («Справочник.X»), prop — имя свойства модуля. Form/Module.bsl
    (модули форм) уходит в forms: их контур — form_get_xml/form_set_xml."""
    root = Path(dump_dir)
    if not root.is_dir():
        raise ValueError("выгрузка не найдена: " + dump_dir)
    # \b: литерал после точки («Документы.X») — граница; «XДругая» — не совпадёт.
    # Superset допустим: цели без переписывания уходят в rewritten[no-change]
    needle = re.compile(r"\b" + re.escape(old_name) + r"\b")
    targets, forms = [], []
    for p in sorted(root.rglob("*.bsl")):
        rel = p.relative_to(root).as_posix()
        try:
            text = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if not needle.search(text):
            continue
        parts = rel.split("/")
        if len(parts) < 3 or parts[0] not in DIR2KIND:
            continue
        kind = DIR2KIND[parts[0]]
        fname = parts[-1]
        if kind == "ОбщаяФорма":
            forms.append(rel)
            continue
        if fname == "Module.bsl" and kind == "ОбщийМодуль":
            obj, prop = parts[1], "Модуль"
        elif fname == "Form" or (len(parts) >= 2 and parts[-2:] == ["Ext", "Module.bsl"]):
            forms.append(rel)
            continue
        else:
            prop = MODULE_FILE2PROP.get(fname)
            obj = parts[1]
            if not prop or parts[2] != "Modules":
                forms.append(rel)
                continue
        targets.append({"rel_path": rel, "path": kind + "." + obj, "prop": prop})
    return targets, forms
