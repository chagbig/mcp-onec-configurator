# -*- coding: utf-8 -*-
"""ref_check — поиск ссылок на объект конфигурации перед удалением (этап 17).

Штатный поиск ссылок конфигуратора уходит в окно и не автоматизируется (этап 13),
поэтому ссылки ищем в XML-контуре: ПОЛНАЯ выгрузка конфигурации (/DumpConfigToFiles)
сканируется на литералы типов удаляемого объекта.

python ref_check.py <каталог-выгрузки> "Справочник.Номенклатура" [--max-context 5]

Почему не cf: в cf-записях ссылки хранятся дескрипторами-uuid (литералов cfg:X.Y
нет), а в XML-выгрузке — литералами. Собственное поддерево объекта (сам объект и
его формы/макеты/модули) исключается ПО ПУТЯМ — точно, без эвристик.

Что ловим: литералы cfg:<Вид>.Имя (Ref/Object/Selection/List/Manager) в реквизитах,
ТЧ, регистрах, формах (DynamicList MainTable/Type), а также MainTable>Вид.Имя.
Ограничения v1: права ролей и подписки в своём формате не сканируются; тексты
запросов в модулях не сканируются (это не структурная ссылка).

Код возврата: 0 — ссылок нет, 2 — найдены (не ошибка), 1 — отказ сканера.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# вид метаданных -> литералы типов 1С (ссылка + производные)
KIND_TYPES = {
    "Справочник": ("CatalogRef", "CatalogObject", "CatalogSelection", "CatalogList", "CatalogManager"),
    "Документ": ("DocumentRef", "DocumentObject", "DocumentSelection", "DocumentList", "DocumentManager"),
    "Перечисление": ("EnumRef", "EnumList", "EnumManager"),
    "ПланВидовХарактеристик": ("ChartOfCharacteristicTypesRef", "ChartOfCharacteristicTypesObject",
                               "ChartOfCharacteristicTypesSelection", "ChartOfCharacteristicTypesList",
                               "ChartOfCharacteristicTypesManager"),
    "ПланСчетов": ("ChartOfAccountsRef", "ChartOfAccountsObject", "ChartOfAccountsSelection",
                   "ChartOfAccountsList", "ChartOfAccountsManager"),
    "ПланВидовРасчета": ("ChartOfCalculationTypesRef", "ChartOfCalculationTypesObject",
                         "ChartOfCalculationTypesSelection", "ChartOfCalculationTypesList",
                         "ChartOfCalculationTypesManager"),
    "ПланОбмена": ("ExchangePlanRef", "ExchangePlanObject", "ExchangePlanSelection",
                   "ExchangePlanList", "ExchangePlanManager"),
    "БизнесПроцесс": ("BusinessProcessRef", "BusinessProcessObject", "BusinessProcessSelection",
                      "BusinessProcessList", "BusinessProcessManager"),
    "Задача": ("TaskRef", "TaskObject", "TaskSelection", "TaskList", "TaskManager"),
    "ОбщаяКоманда": ("CommonCommand",),
    "ОбщийМакет": ("CommonTemplate",),
    "ОбщаяФорма": ("CommonForm",),
    "Роль": ("Role",),
    "ОпределяемыйТип": ("DefinedType",),
}


# русский вид -> английское имя вида (MainTable/QualifiedNames в XML)
KIND_EN = {
    "Справочник": "Catalog", "Документ": "Document", "Перечисление": "Enum",
    "ПланВидовХарактеристик": "ChartOfCharacteristicTypes", "ПланСчетов": "ChartOfAccounts",
    "ПланВидовРасчета": "ChartOfCalculationTypes", "ПланОбмена": "ExchangePlan",
    "БизнесПроцесс": "BusinessProcess", "Задача": "Task",
    "ОбщаяКоманда": "CommonCommand", "ОбщийМакет": "CommonTemplate",
    "ОбщаяФорма": "CommonForm", "Роль": "Role", "ОпределяемыйТип": "DefinedType",
}


def type_patterns(full_name: str) -> list[str]:
    """Литералы для «Вид.Имя»: cfg:CatalogRef.X и производные + базовый вид
    (Document.X — MainTable динамических списков)."""
    kind, _, name = full_name.partition(".")
    prefixes = KIND_TYPES.get(kind, ())
    en = KIND_EN.get(kind)
    if not prefixes or not name:
        return []
    pats = [f"cfg:{p}.{name}" for p in prefixes] + [f"{p}.{name}" for p in prefixes]
    if en:
        pats += [f"cfg:{en}.{name}", f"{en}.{name}"]
    pats.append(f"{kind}.{name}")  # составы подсистем, поддержка: «Документ.МсрТЧ43»
    return pats

OWN_DIRS = {
    "Справочник": "Catalogs", "Документ": "Documents", "Перечисление": "Enums",
    "ПланВидовХарактеристик": "ChartsOfCharacteristicTypes", "ПланСчетов": "ChartsOfAccounts",
    "ПланВидовРасчета": "ChartsOfCalculationTypes", "ПланОбмена": "ExchangePlans",
    "БизнесПроцесс": "BusinessProcesses", "Задача": "Tasks",
    "ОбщаяКоманда": "CommonCommands", "ОбщийМакет": "CommonTemplates",
    "ОбщаяФорма": "CommonForms", "Роль": "Roles", "ОпределяемыйТип": "DefinedTypes",
}


def own_subtree(dump: Path, full_name: str) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Собственное поддерево объекта: (файлы объекта, каталоги форм/макетов/модулей)."""
    kind, _, obj = full_name.partition(".")
    d = OWN_DIRS.get(kind)
    if not d or not obj:
        return (), ()
    base = dump / d
    return (base / (obj + ".xml"),), (base / obj,)


def scan_dump(dump: Path, full_name: str, max_context: int = 5) -> dict:
    """Скан XML-выгрузки на литералы типов объекта (кроме собственного поддерева)."""
    patterns = type_patterns(full_name)
    if not patterns:
        return {"ok": False, "err": f"неизвестный вид или пустое имя: {full_name!r}"}
    own_files, own_dirs = own_subtree(dump, full_name)
    hits: list[dict] = []
    files_scanned = 0
    if not dump.is_dir():
        return {"ok": False, "err": f"выгрузка не найдена: {dump}"}
    for p in sorted(dump.rglob("*.xml")) + sorted(dump.rglob("*.bsl")):
        if p.name == "ConfigDumpInfo.xml":
            continue  # служебный файл выгрузки: перечисляет и собственное поддерево
        if p in own_files:
            continue
        if any(p.is_relative_to(d) for d in own_dirs if d.exists()):
            continue
        try:
            text = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        files_scanned += 1
        for line_no, line in enumerate(text.splitlines(), 1):
            for pat in patterns:
                if pat in line:
                    hits.append({"file": p.relative_to(dump).as_posix(),
                                 "line": line_no, "pattern": pat,
                                 "context": line.strip()[:200]})
                    break
            if len(hits) >= 100:
                break
        if len(hits) >= 100:
            break
    truncated = len(hits) >= 100
    hits = hits[:max_context]
    return {"ok": True, "target": full_name, "patterns": patterns,
            "files_scanned": files_scanned, "refs_found": bool(hits),
            "truncated": truncated, "hits": hits}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Поиск ссылок на объект в XML-выгрузке")
    ap.add_argument("dump", help="каталог полной выгрузки (/DumpConfigToFiles)")
    ap.add_argument("object", help="Вид.Имя, напр. Справочник.Номенклатура")
    ap.add_argument("--max-context", type=int, default=5)
    a = ap.parse_args(argv)
    res = scan_dump(Path(a.dump), a.object, a.max_context)
    if not res["ok"]:
        print("FAIL", res["err"])
        return 1
    print(f"файлов просканировано: {res['files_scanned']}")
    print("ссылки:", "НАЙДЕНЫ" if res["refs_found"] else "не найдены")
    for h in res["hits"]:
        print(f"  {h['file']}:{h['line']} [{h['pattern']}] {h['context']}")
    return 2 if res["refs_found"] else 0


if __name__ == "__main__":
    sys.exit(main())
