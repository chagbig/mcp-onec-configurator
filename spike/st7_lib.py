"""Этап 7: создание объектов через дерево метаданных — общая часть сценариев опытов.

Навигация до узла группы подчинённых детерминирована: выделить владельца → «+» (развернуть) →
k раз «↓, −» (каждый пройденный узел свернуть) → Insert. Номер группы k — по классу владельца.
"""
import sys

sys.path.insert(0, "spike")
import client  # noqa: E402

DOWN, ADD, SUB = 0x28, 0x6B, 0x6D

# Порядок узлов групп в дереве (1 — первый узел под владельцем); снято с экрана 17.10.2026
GROUP_INDEX = {
    "Конфигурация": {"Константы": 2, "Справочники": 3, "Документы": 4, "ЖурналыДокументов": 5, "Перечисления": 6,
                     "Отчеты": 7, "Обработки": 8, "ПланыВидовХарактеристик": 9, "ПланыСчетов": 10,
                     "ПланыВидовРасчета": 11, "РегистрыСведений": 12, "РегистрыНакопления": 13,
                     "РегистрыБухгалтерии": 14, "РегистрыРасчета": 15, "БизнесПроцессы": 16, "Задачи": 17},
    "Справочник": {"Реквизиты": 1, "ТабличныеЧасти": 2, "Формы": 3, "Команды": 4, "Макеты": 5},
    "Документ": {"Реквизиты": 1, "ТабличныеЧасти": 2, "Формы": 3, "Команды": 4, "Макеты": 5},
}
SINGULAR = {"Справочники": "Справочник", "Документы": "Документ", "Перечисления": "Перечисление",
            "РегистрыСведений": "РегистрСведений", "Реквизиты": "Реквизит", "ТабличныеЧасти": "ТабличнаяЧасть",
            "Измерения": "Измерение", "Ресурсы": "Ресурс", "ЗначенияПеречисления": "ЗначениеПеречисления",
            "Константы": "Константа"}

log = []


def focus(path):
    for i in range(3):
        client.call("tree_activate", {"path": path}, timeout=60)
        av = (client.call("active_view", {}, timeout=30).get("result") or {}).get("title")
        log.append(f"activate «{path}» try{i}: {av}")
        if av and av.startswith("Конфигурация"):
            return True
    return False


def add(owner_path, owner_class, kind, name, comment=None, sibling=None, group_index=None):
    """Создать подчинённый вида kind. sibling — рядом с существующим, иначе — навигация до узла группы."""
    expect = SINGULAR.get(kind)
    if sibling:
        focus((owner_path + "." if owner_path else "") + SINGULAR[kind] + "." + sibling)
        args = {"sibling": sibling}
    else:
        k = group_index or GROUP_INDEX[owner_class][kind]
        focus(owner_path)
        # k — число или путь из номеров по уровням (подсистемы: [1, 1] = «Общие» → «Подсистемы»)
        path = k if isinstance(k, list) else [k]
        nav = []
        for step in path:
            nav += [ADD] + [DOWN, SUB] * step
        args = {"firstChild": True, "navKeys": nav}
    args.update({"ownerPath": owner_path, "kind": kind, "method": "key", "noActivate": True, "requireTree": True,
                 "expectClass": expect, "newName": name, "comment": comment})
    r = client.call("md_add_child", args, timeout=180)
    res = r.get("result") or {}
    return {"ok": r["ok"] and bool(res.get("renamed")), "err": r.get("error"), "before": res.get("before"),
            "after": res.get("after"), "id": (res.get("created") or {}).get("id"), "renamed": res.get("renamed"),
            "classMismatch": res.get("classMismatch"), "wrong": res.get("wrong"), "stray": res.get("stray"),
            "ms": r.get("ms")}


def set_type(path, obj_id, **t):
    r = client.call("prop_set_type", {"path": path, "expectId": obj_id, "type": t}, timeout=60)
    return (r.get("result") or {}).get("after") or r.get("error")
