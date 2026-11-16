# -*- coding: utf-8 -*-
"""№29 продолжение (v1.15): UIA-дерево конфигурации — навигация без
снегопат-курсора.

Дерево окна «Конфигурация» экспонируется через UIA: ListControl с
ListItemControl на каждую ветку (Name = имя ветки, IsEnabled, Expand-
Collapse-состояние на «+» узлах). Проверено живьём (v1.15):
- scan: все ветки верхнего уровня с именами и BoundingRectangle;
- select: клик мышью из тика по центру BoundingRectangle — узел выделяется
  (координаты точные из UIA, не слепые);
- expand: выделить + VK_ADD (серый плюс) — ветка раскрывается;
- collapse: выделить + VK_SUBTRACT (серый минус).

Важно: UIA-скан отражает дерево с задержкой отрисовки (после раскрытия
сканировать повторно через паузу). Соседний редактор (StndlEDO) не мешает.
Зависимость: uiautomation (Windows-only).
"""
from __future__ import annotations

import json
import sys
import time

VK_ADD = 107       # серый плюс — раскрыть ветку
VK_SUBTRACT = 109  # серый минус — свернуть ветку


def find_item(hwnd: int, name: str, partial: bool = False):
    """Найти ListItemControl по имени (точное или подстрока).
    Возврат элемента или None."""
    import uiautomation as auto
    ctrl = auto.ControlFromHandle(hwnd)
    if not ctrl:
        raise RuntimeError("UIA: главное окно не найдено (hwnd=%s)" % hwnd)
    target = None
    def walk(c, d):
        nonlocal target
        if target is not None or d > 12:
            return
        try:
            for k in c.GetChildren():
                nm = (k.Name or "").strip()
                if k.ControlTypeName == "ListItemControl" and (
                        nm == name if not partial else name.lower() in nm.lower()):
                    target = k
                    return
                walk(k, d + 1)
        except Exception:
            pass
    walk(ctrl, 0)
    return target


def scan_top(hwnd: int) -> list:
    """Верхний уровень дерева: [{name, enabled, expandable, state}].
    state: 0=collapsed, 1=expanded (ExpandCollapseState), -1=нет узла."""
    import uiautomation as auto
    ctrl = auto.ControlFromHandle(hwnd)
    if not ctrl:
        raise RuntimeError("UIA: главное окно не найдено")
    out = []
    def walk(c, d):
        if d > 12 or len(out) > 60:
            return
        try:
            for k in c.GetChildren():
                if k.ControlTypeName == "ListItemControl" and (k.Name or "").strip():
                    ec = k.GetPattern(auto.PatternId.ExpandCollapsePattern)
                    out.append({
                        "name": (k.Name or "").strip(),
                        "enabled": bool(k.IsEnabled),
                        "state": (ec.ExpandCollapseState if ec else -1),
                        "rect": [k.BoundingRectangle.left, k.BoundingRectangle.top,
                                 k.BoundingRectangle.right, k.BoundingRectangle.bottom],
                    })
                walk(k, d + 1)
        except Exception:
            pass
    walk(ctrl, 0)
    return out


def center_of(item) -> tuple:
    r = item.BoundingRectangle
    return int((r.left + r.right) / 2), int((r.top + r.bottom) / 2)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    import argparse
    ap = argparse.ArgumentParser(description="uia_tree: скан дерева конфигурации")
    ap.add_argument("--hwnd", type=int, required=True)
    ap.add_argument("--out", default="", help="файл JSON результата")
    a = ap.parse_args(argv)
    try:
        items = scan_top(a.hwnd)
    except Exception as e:
        print("ОТКАЗ: %s" % e, file=sys.stderr)
        return 2
    out = json.dumps({"items": items}, ensure_ascii=False, indent=1)
    if a.out:
        from pathlib import Path
        Path(a.out).write_text(out, encoding="utf-8")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
