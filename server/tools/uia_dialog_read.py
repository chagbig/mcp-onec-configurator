# -*- coding: utf-8 -*-
"""UIA-скан открытого диалога/окна конфигуратора (план v1.14, Фаза 0).

Цель — «что пользователь заполнил в диалоге создания объекта»: поля диалога
1С экспонирует через UI Automation С ИМЕНАМИ (Фаза 0, docs/uia-phase0-recon.md:
пункты меню — Name+IsEnabled точны; диалоги предположительно тоже — живая
проверка на стенде владельца). Модуль ТОЛЬКО ЧИТАЕТ (ControlFromHandle +
обход дерева) — ничего не активирует (Invoke на кастомном меню 1С
игнорируется, активация остаётся за тиком моста: mouse_click + клавиатура).

Сторона станции: uiautomation 2.0.29 (чистая обёртка над UIA COM, уже в
окружении пробы). Windows-only: на других платформах read_dialog и CLI
поднимают RuntimeError с понятным текстом (импорт uiautomation/win32 —
ленивый, после гварда, поэтому сам модуль импортируется и на Linux).

Порядок: read_dialog(pid, [title_contains]) —
  1) EnumWindows по pid, видимые окна верхнего уровня
     (window_shot.list_windows); выбирается ГЛАВНОЕ окно (максимальная
     площадь) или окно по подстроке заголовка (--title / title_contains —
     целевой диалог конфигуратора тоже окно верхнего уровня);
  2) обход дерева UIA от корня: глубина <= MAX_DEPTH (12), счётчик
     <= MAX_CONTROLS (600) посещённых контролов; списки в порядке документа
     (pre-order); бюджет исчерпан -> truncated=true;
  3) сбор:
     fields  — EditControl/ComboBoxControl/DocumentControl/CheckBoxControl/
               TabItemControl/TreeControl: {type, name, value}; value —
               ValuePattern (GetPattern(PatternId.ValuePattern).Value),
               паттерна нет -> "";
     buttons — ButtonControl с НЕпустым Name: {name, enabled(IsEnabled)};
     title/class — Name/ClassName корневого контрола (fallback — заголовок
               окна из EnumWindows).

CLI: python tools/uia_dialog_read.py --pid <pid> [--title SUBSTR] [--out res.json]
rc=0 успех (JSON в stdout или --out), rc=2 — ошибка окружения (не Windows,
нет видимых окон, окно по --title не найдено).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# CLI-запуск (в т.ч. python -I): сосед window_shot должен быть импортируемым
sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Максимальная глубина обхода дерева UIA (уровней контролов от корня).
MAX_DEPTH = 12

#: Бюджет посещённых контролов на один скан (защита от гигантских деревьев).
MAX_CONTROLS = 600

#: Типы контролов (ControlTypeName), попадающие в fields.
FIELD_TYPES = frozenset({
    "EditControl", "ComboBoxControl", "DocumentControl",
    "CheckBoxControl", "TabItemControl", "TreeControl",
})

#: Тип контролов, попадающий в buttons (Name обязателен — пустые пропускаются).
BUTTON_TYPE = "ButtonControl"


def require_windows() -> None:
    """Гвард платформы: не Windows -> RuntimeError с понятным текстом."""
    if sys.platform != "win32":
        raise RuntimeError(
            "uia_dialog_read работает только на Windows: UI Automation "
            "(COM, пакет uiautomation) недоступен на платформе %r"
            % sys.platform)


def _control_info(ctrl, value_pattern_id: int) -> dict:
    """{type, name, value, enabled} одного контрола.

    Каждый — даже исчезнувший — контрол даёт запись (пропускается только
    недочитанное свойство), поэтому счётчик бюджета всегда честный.
    """
    info = {"type": "", "name": "", "value": "", "enabled": False}
    for key, getter in (("type", lambda: str(ctrl.ControlTypeName)),
                        ("name", lambda: str(ctrl.Name or "")),
                        ("enabled", lambda: bool(ctrl.IsEnabled))):
        try:
            info[key] = getter()
        except Exception:
            pass  # элемент исчез / свойство недоступно
    if info["type"] in FIELD_TYPES:
        try:
            pat = ctrl.GetPattern(value_pattern_id)
            if pat is not None:
                info["value"] = str(pat.Value or "")
        except Exception:
            info["value"] = ""
    return info


def read_dialog(pid: int, title_contains: str = "",
                max_depth: int = MAX_DEPTH,
                max_controls: int = MAX_CONTROLS) -> dict:
    """Скан открытого диалога/окна процесса pid (см. докстринг модуля).

    Возврат: {pid, hwnd, title, class,
              fields: [{type, name, value}],
              buttons: [{name, enabled}],
              controls_scanned, truncated}.
    title_contains — выбрать окно по подстроке заголовка (иначе главное,
    максимальная площадь). Не Windows -> RuntimeError; нет окон -> RuntimeError;
    окно по подстроке не найдено -> FileNotFoundError.
    """
    require_windows()
    from window_shot import list_windows  # pywin32 — после гварда платформы
    wins = list_windows(pid)
    if title_contains:
        cand = [w for w in wins
                if title_contains.lower() in w["title"].lower()]
        if not cand:
            raise FileNotFoundError(
                "окно с «%s» не найдено у pid %d (видимых окон: %d)"
                % (title_contains, pid, len(wins)))
        win = max(cand, key=lambda w: len(w["title"]))  # конкретнее — длиннее
    else:
        if not wins:
            raise RuntimeError(
                "у процесса %d нет видимых окон верхнего уровня" % pid)
        win = max(wins, key=lambda w: (w["rect"][2] - w["rect"][0])
                                      * (w["rect"][3] - w["rect"][1]))

    import uiautomation as auto
    root = auto.ControlFromHandle(win["hwnd"])
    if root is None:
        raise RuntimeError(
            "UIA не открыл окно hwnd=%s (pid %d)" % (win["hwnd"], pid))
    value_pattern_id = auto.PatternId.ValuePattern

    fields: list = []
    buttons: list = []
    budget = {"count": 0, "truncated": False}

    def walk(ctrl, depth: int) -> None:
        if budget["count"] >= max_controls:
            budget["truncated"] = True
            return
        budget["count"] += 1
        info = _control_info(ctrl, value_pattern_id)
        if info["type"] in FIELD_TYPES:
            fields.append({"type": info["type"], "name": info["name"],
                           "value": info["value"]})
        elif info["type"] == BUTTON_TYPE and info["name"]:
            buttons.append({"name": info["name"], "enabled": info["enabled"]})
        if depth >= max_depth:
            return  # сам контрол учтён, вниз не спускаемся
        try:
            child = ctrl.GetFirstChildControl()
        except Exception:
            child = None
        while child is not None:
            walk(child, depth + 1)
            if budget["count"] >= max_controls:
                return
            try:
                child = child.GetNextSiblingControl()
            except Exception:
                child = None

    walk(root, 0)
    title, cls = "", ""
    try:
        title = str(root.Name or "")
        cls = str(root.ClassName or "")
    except Exception:
        pass
    return {"pid": pid,
            "hwnd": win["hwnd"],
            "title": title or win["title"],
            "class": cls,
            "fields": fields,
            "buttons": buttons,
            "controls_scanned": budget["count"],
            "truncated": budget["truncated"]}


def main(argv: list | None = None) -> int:
    """CLI: --pid (обязателен), --title <подстрока>, --out <json>; rc=0/2."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description="UIA-скан открытого диалога/окна конфигуратора: "
                    "fields/buttons/title/class (только чтение, без активации)")
    ap.add_argument("--pid", type=int, required=True,
                    help="pid процесса 1cv8 (конфигуратора)")
    ap.add_argument("--title", default="",
                    help="подстрока заголовка окна (иначе — главное окно процесса)")
    ap.add_argument("--out", default="",
                    help="путь JSON-вывода (по умолчанию stdout)")
    args = ap.parse_args(argv)
    try:
        res = read_dialog(args.pid, args.title)
    except (RuntimeError, FileNotFoundError) as e:
        print("uia_dialog_read: %s" % e, file=sys.stderr)
        return 2
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
