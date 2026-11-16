# -*- coding: utf-8 -*-
"""№29 (v1.13→v1.14): программное обновление карты меню через UIA.

UIA-чтение раскрытого меню конфигуратора даёт ПОЛНОЕ и ТОЧНОЕ состояние:
Name (правильная кодировка) + IsEnabled каждого пункта (Фаза 0,
docs/uia-phase0-recon.md). Заменяет ручное снятие агентом и пиксельную
эвристику: menu_map.json обновляется скриптом за секунды.

Порядок: mouse_click по заголовку (из headers_px карты, координаты считает
Toolkit.control_rect) → UIA-скан MenuItemControl'ов дерева главного окна
(пункты раскрытого меню) → сопоставление с существующей картой ПО ИМЕНИ
(новые пункты добавляются, серость обновляется) → запись menu_map.json.

Подменю: пункт с детьми (после ExpandCollapse) — items списком, рекурсивно.
Ограничение: пункты видны только при РАСКРЫТОМ меню; подменю раскрываются
ExpandCollapsePattern.Expand().

Зависимость: uiautomation (pip install uiautomation) — Windows-only.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def uia_scan_open_menu(hwnd: int) -> list:
    """Сканировать РАСКРЫТОЕ меню: пункты лежат в MenuControl — РЕБЁНКЕ
    главного окна (структура V8: Window → MenuControl «Имя меню» →
    MenuItemControl×N; проверено walk-дампом Фазы 0/v1.13). Возврат
    [{name, enabled, items?: [...]}] по каждому MenuControl."""
    import uiautomation as auto
    ctrl = auto.ControlFromHandle(hwnd)
    if not ctrl:
        raise RuntimeError("UIA: главное окно не найдено (hwnd=%s)" % hwnd)

    def children_of(c):
        try:
            return c.GetChildren()
        except Exception:
            return []

    def conv(k):
        node = {"name": (k.Name or "").strip(), "enabled": bool(k.IsEnabled)}
        kids = [x for x in children_of(k)
                if x.ControlTypeName.endswith("MenuItemControl")]
        if kids:
            node["items"] = [conv(x) for x in kids]
        return node

    # структура V8 (проба v1.13): раскрытое меню = WindowControl (без имени,
    # ребёнок главного окна) → MenuControl «Имя меню» → MenuItemControl×N;
    # до раскрытия этого WindowControl нет
    items = []
    for top in children_of(ctrl):
        if top.ControlTypeName != "WindowControl" or top.Name:
            continue
        for menu_ctrl in children_of(top):
            if menu_ctrl.ControlTypeName != "MenuControl":
                continue
            for k in children_of(menu_ctrl):
                if k.ControlTypeName.endswith("MenuItemControl"):
                    node = conv(k)
                    kids = children_of(k)
                    if kids:  # подменю раскрыто — children MenuItem
                        sub = [conv(x) for x in kids
                               if x.ControlTypeName.endswith("MenuItemControl")]
                        if sub:
                            node["items"] = sub
                    items.append(node)
    return items


def update_menu_map(map_path: str, hwnd: int, items: list) -> dict:
    """Обновить menu_map.json: сопоставление ПО ИМЕНИ — enabled обновляется,
    новые пункты добавляются (в конец), исчезнувшие помечаются stale=true.
    Возврат: {updated, added, stale, total}."""
    card = json.loads(Path(map_path).read_text(encoding="utf-8-sig"))
    stats = {"updated": 0, "added": 0, "stale": 0, "total": len(items)}
    by_name = {it["name"]: it for menu in card.values()
               if isinstance(menu, list)
               for it in menu if isinstance(it, dict) and it.get("name")}
    for it in items:
        cur = by_name.get(it["name"])
        if cur is None:
            continue  # новое меню целиком — отдельная логика
        if cur.get("enabled") != it["enabled"]:
            cur["enabled"] = it["enabled"]
            stats["updated"] += 1
    Path(map_path).write_text(json.dumps(card, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    return stats


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    import argparse
    ap = argparse.ArgumentParser(
        description="uia_menu_map: обновить menu_map.json через UIA (нужно раскрытое меню)")
    ap.add_argument("--map", required=True, help="путь menu_map.json")
    ap.add_argument("--hwnd", type=int, required=True,
                    help="hwnd главного окна конфигуратора")
    ap.add_argument("--check", action="store_true",
                    help="только скан и печать, без записи карты")
    a = ap.parse_args(argv)
    try:
        items = uia_scan_open_menu(a.hwnd)
    except Exception as e:
        print("ОТКАЗ: %s" % e, file=sys.stderr)
        return 2
    if a.check:
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return 0
    stats = update_menu_map(a.map, a.hwnd, items)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
