# -*- coding: utf-8 -*-
"""make_snegopat_set — включить мост в список пользовательских аддинов Снегопата.

Работает с файлом настроек `snegopat.set` (формат: построчно `Ключ<TAB>
сериализованное ValueTree`). Ключ `Snegopat/LoadAddins` — дерево загрузки
пользовательских аддинов, которое при старте читает select_scripts.loadUserScripts().
Заводской .set содержит полный список стандартных аддинов; мост добавляется
ХИРУРГИЧЕСКИ: лист `{"S","script:addins\\mcp\\mcp_bridge.js"},{"B",0},0`
с новым индексом ряда + инкремент счётчика в хвосте дерева (`},-1,<N>}`).

CLI:
  python make_snegopat_set.py --src <snegopat.set> --addin "script:addins\\mcp\\mcp_bridge.js" [--out <файл>]
Без --out — перезаписывает --src (idempotent: лист уже есть — файл не меняется).
Код возврата: 0 — ок (в т.ч. уже был), 1 — не удалось.

Формат (снято с живых файлов 1.6.43 и 3.0.66):
  хвост дерева лист-ряда: `{"B",0},0}` затем `\n},-1,<последний индекс ряда>}\n}\n}`
"""
from __future__ import annotations

import argparse
import re
import sys

BRIDGE_LOADER_DEFAULT = r"script:addins\mcp\mcp_bridge.js"

# хвост дерева: последний ряд-лист + закрывающие },-1,<N>}\n}\n}. ВАЖНО: сериализат —
# ОДНА строка, «переводы строк» в нём — литеральные пары «\»+«n» (не символы 0x0A);
# после -1,<N> закрывающая скобка ровно одна, дальше лит-\n, }, лит-\n, }.
# Регекс собран КОНКАТЕНАЦИЕЙ проверенных кусков (ручной текст трижды искажал
# бэкслеши при правках — бисекция по кускам в probe/dbg_set10 все ok).
_P_ROW_OPEN = r"(?P<row>"
_P_IDX = r"\{2," + r"(?P<idx>\d+)" + r",2," + r"\\n" + r'\{"S","'
_P_LOADER = r"(?P<loader>[^\"]+)"
_P_ROW_CLOSE = r'"\},' + r"\\n" + r'\{"B",0\},0\}' + r")"
_P_TAIL = r"\\n" + r"\},-1," + r"(?P<last>\d+)" + r"\}" + r"\\n" + r"\}" + r"\\n" + r"\}$"
_TAIL_RE = re.compile(_P_ROW_OPEN + _P_IDX + _P_LOADER + _P_ROW_CLOSE + _P_TAIL)


def add_leaf(set_text: str, loader: str = BRIDGE_LOADER_DEFAULT) -> tuple[str, bool, str]:
    """Вернуть (новый текст, изменён ли, сообщение)."""
    lines = set_text.splitlines()
    target_idx = None
    for i, line in enumerate(lines):
        if line.startswith("Snegopat/LoadAddins\t"):
            target_idx = i
            break
    if target_idx is None:
        return set_text, False, "в snegopat.set нет ключа Snegopat/LoadAddins"
    key, _, value = lines[target_idx].partition("\t")
    # в сериализате бэкслеши путей удвоены: script:addins\\mcp\\mcp_bridge.js
    if loader.replace("\\", "\\\\") in value:
        return set_text, False, "лист уже есть — файл не менялся"
    m = _TAIL_RE.search(value)
    if not m:
        return set_text, False, (
            "не найден хвост дерева (последний ряд-лист + },-1,N}}) — "
            "список пуст или формат незнаком; добавьте аддин через UI Снегопата")
    new_idx = int(m.group("idx")) + 1
    # счётчик строк верхнего уровня: {2,N,0,0,1,1,\n{1,<count>, — новый лист
    # добавляется В КОНЕЦ верхнего уровня, счётчик обязан вырасти, иначе парсер
    # ValueTree читает ровно <count> рядов и лист молча отбрасывается (3.0.66)
    m_parent = re.search(r'\{2,\d+,0,0,1,1,\\n\{1,(\d+),', value)
    if not m_parent:
        return set_text, False, (
            "не найден счётчик верхнего уровня ({2,N,0,0,1,1,\\n{1,N,) — "
            "формат дерева незнаком")
    new_count = int(m_parent.group(1)) + 1
    s1, e1 = m_parent.span(1)
    value = value[:s1] + str(new_count) + value[e1:]  # инкремент счётчика верхних рядов
    leaf = ("{2,%d,2,\\n" % new_idx
            + '{"S","%s"},\\n{"B",0},0}' % loader.replace("\\", "\\\\"))
    tail_start = m.start("row")
    new_value = (value[:tail_start] + m.group("row") + ",\\n" + leaf
                 + "\\n},-1,%d}\\n}\\n}" % new_idx
                 + value[m.end():])
    lines[target_idx] = key + "\t" + new_value
    return "\n".join(lines) + ("\n" if set_text.endswith("\n") else ""), True, (
        "добавлен лист %s (индекс ряда %d, верхний уровень: %d)"
        % (loader, new_idx, new_count))


def fresh_set(loader: str = BRIDGE_LOADER_DEFAULT) -> str:
    """Минимальный .set для сборки БЕЗ заводского списка: одно дерево LoadAddins
    с одним листом-мостом. Формат хвоста идентичен живым файлам (лит-\\n)."""
    leaf = r"{2,0,2," + r"\n" + '{"S","%s"},\\n{"B",0},0}' % loader.replace("\\", "\\\\")
    tree = (r'{"#",e603c0f2-92fb-4d47-8f38-a44a381cf235,\n{1,\n{2,'
            r'\n{0,"Addin",\n{"Pattern"},"",0},\n{1,"isGroup",\n{"Pattern"},"",0}\n},'
            r'\n{2,1,0,0,1,1,\n{1,1,' + leaf + r'\n},-1,0}\n}\n}')
    return "Snegopat/LoadAddins\t" + tree + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Добавить мост в snegopat.set")
    ap.add_argument("--src", default="", help="файл snegopat.set (заводской или живой)")
    ap.add_argument("--addin", default=BRIDGE_LOADER_DEFAULT)
    ap.add_argument("--out", default="", help="по умолчанию — перезаписать --src")
    ap.add_argument("--fresh", action="store_true",
                    help="создать минимальный .set с одним листом-мостом (без --src)")
    a = ap.parse_args(argv)
    if a.fresh:
        out = a.out or "snegopat.set"
        with open(out, "wb") as fh:
            fh.write(fresh_set(a.addin).encode("utf-8"))
        print("ok: минимальный .set с мостом ->", out)
        return 0
    if not a.src:
        print("FAIL: укажите --src или --fresh")
        return 1
    text = open(a.src, "rb").read().decode("utf-8")
    new_text, changed, msg = add_leaf(text, a.addin)
    if not changed and "уже есть" not in msg:
        print("FAIL", msg)
        return 1
    out = a.out or a.src
    with open(out, "wb") as fh:
        fh.write(new_text.encode("utf-8"))
    print("ok:", msg, "->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
