"""Парсер структуры модуля 1С (BSL): процедуры/функции с номерами строк.

IDEAS №16. bridge module_patch правит модуль по номерам строк, а берут их
сейчас «вслепую» (module_get + пересчёт глазами). Эти функции дают точную карту:

  parse(text)                -> [{name, kind, line_start, line_end,
                                 directives, export, params}, ...]
  find_call_sites(text, имя) -> [{line, fragment}, ...]

Чистый python без внешних зависимостей. Вход — текст модуля, как его
возвращает bridge module_get (with_text=true): EOL может быть \\r\\n или \\n —
нормализуется к \\n, номера строк 1-базные и совпадают с номерами строк
bridge module_patch (который правит по ним).

Эвристика объявления (MVP; полный лексер не нужен):
  строка-объявление начинается (после возможных пробелов) с токена
  «Процедура»/«Функция» С ГРАНИЦЕЙ СЛОВА (так «ПроцедурыНабора(х)» не
  объявление) и далее в строке до конца есть открывающая «(»; имя — первый
  идентификатор после токена, «Экспорт» — слово сразу после закрывающей «)»
  списка параметров. Комментарии (//...), строки-продолжения литералов (|...)
  и литералы, СОДЕРЖАЩИЕ слово «Процедура», блок не создают: их строка не
  начинается с токена. «Функция» без «(» — пропускается (не объявление).
  Токен без имени («Процедура (х)») — тоже пропускается: блок без имени
  бесполезен для точечных правок.

Осознанные ограничения MVP:
  - директивы компиляции (&НаКлиенте, &НаСервере, &НаСервереБезКонтекста,
    &НаКлиентеНаСервере, ...) распознаются по общему виду &Имя и привязываются
    к блоку, идя «вверх» от строки Процедуры через пустые строки и комментарии
    (директива в BSL всегда относится к следующему объявлению, так что ошибка
    привязки исключена); стоп — на любом другом содержимом (Перем, Конец...,
    #Область);
  - params берётся из строки объявления: перенос списка параметров на
    следующие строки усекается до конца строки объявления, «Экспорт» после
    переноса не распознаётся (export=false);
  - «(» в хвостовом комментарии строки объявления может дать мусорный params
    (эвристика — «есть «(» до конца строки», не лексер);
  - вложенных процедур в BSL нет — объявление внутри открытого блока
    игнорируется; незакрытый блок -> line_end=None;
  - конец блока — первая строка «КонецПроцедуры|КонецФункции» (тип конца не
    сверяется с kind: расхождение — синтаксическая ошибка, платформа такое не
    вернёт);
  - find_call_sites не разбирает строковые литералы внутри строки кода:
    упоминание «Имя(» в литерале будет посчитано вызовом (MVP).

find_call_sites(text, name): собственные вызовы процедуры/функции в модуле —
строки «Имя(», не являющиеся объявлением, Конец.../комментарием/продолжением
литерала; один результат на строку (fragment — строка без краевых пробелов);
доступ через точку (ЧужойОбъект.Имя(...)) не считается — это вызов метода
другого объекта, для иерархии вызовов в пределах модуля он шум; имя
сравнивается без учёта регистра (в BSL регистр идентификаторов не различается).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

__all__ = ["parse", "find_call_sites"]

# Идентификатор BSL: буквы (кириллица/латиница), цифры, «_», не начинается с цифры
_IDENT = r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*"
_IDENT_RE = re.compile(_IDENT)

# Строка-объявление: начинается с токена Процедура/Функция с границей слова
_DECL_RE = re.compile(r"^\s*(Процедура|Функция)\b")
# Имя объявления: идентификатор сразу после токена
_DECL_NAME_RE = re.compile(r"^\s*(?:Процедура|Функция)\s+(" + _IDENT + r")")
# Конец блока
_END_RE = re.compile(r"^\s*Конец(?:Процедуры|Функции)\b")
# Директива компиляции: &НаКлиенте, &НаСервере, ... (общий вид &Имя)
_DIRECTIVE_RE = re.compile(r"^\s*(&" + _IDENT + r")")
# «Экспорт» как первое слово после закрывающей «)» списка параметров
_EXPORT_RE = re.compile(r"^\s*Экспорт\b")


def _normalize(text: str) -> str:
    """BOM и EOL: \\r\\n и одиночный \\r -> \\n (номера строк не меняются)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _is_decl_line(raw: str) -> bool:
    """Эвристика объявления: токен Процедура/Функция в начале строки (после
    возможных пробелов) и «(» где-то в остатке строки."""
    m = _DECL_RE.match(raw)
    return bool(m) and "(" in raw[m.end():]


def _collect_directives(lines: list[str], decl_idx: int) -> list[str]:
    """Директивы непосредственно перед строкой объявления (decl_idx — 0-базный):
    идём вверх через пустые строки и комментарии, собираем &Имя; стоп на любом
    другом содержимом."""
    found: list[str] = []
    j = decl_idx - 1
    while j >= 0:
        stripped = lines[j].strip()
        if not stripped or stripped.startswith("//"):
            j -= 1
            continue
        m = _DIRECTIVE_RE.match(lines[j])
        if m:
            found.insert(0, m.group(1))
            j -= 1
            continue
        break
    return found


def _extract_params(line: str, open_idx: int) -> tuple[str, int | None]:
    """Строка параметров между «(» и парной «)»; строковые литералы в значениях
    по умолчанию (в т.ч. с «)» внутри) учитываются. Незакрытый список
    (перенос параметров на следующие строки) — до конца строки объявления."""
    if open_idx < 0:
        return "", None
    depth = 0
    in_str = False
    i = open_idx
    n = len(line)
    while i < n:
        ch = line[i]
        if in_str:
            if ch == '"':
                if i + 1 < n and line[i + 1] == '"':
                    i += 2  # «""» — экранированная кавычка внутри литерала
                    continue
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return line[open_idx + 1:i], i
        i += 1
    return line[open_idx + 1:].rstrip(), None


def parse(text: str) -> list[dict]:
    """Разобрать структуру модуля: блоки в порядке появления.

    Элемент: {name, kind («Процедура»|«Функция»), line_start (строка
    «Процедура...», 1-базная), line_end (строка КонецПроцедуры/КонецФункции,
    None у незакрытого блока), directives (['&НаСервере', ...] — директивы,
    привязанные к блоку), export (bool), params (строка параметров без имени,
    '' если параметров нет)}.
    """
    lines = _normalize(text).split("\n")
    blocks: list[dict] = []
    current: dict | None = None
    for idx, raw in enumerate(lines):
        if current is None:
            if _is_decl_line(raw):
                name_m = _DECL_NAME_RE.match(raw)
                if not name_m:
                    continue  # токен без имени («Процедура (х)») — не блок
                open_idx = raw.find("(", name_m.end())
                params, close_idx = _extract_params(raw, open_idx)
                rest = raw[close_idx + 1:] if close_idx is not None else ""
                current = {
                    "name": name_m.group(1),
                    "kind": _DECL_RE.match(raw).group(1),  # «Процедура»|«Функция»
                    "line_start": idx + 1,
                    "line_end": None,
                    "directives": _collect_directives(lines, idx),
                    "export": bool(_EXPORT_RE.match(rest)),
                    "params": params,
                }
            continue
        # Внутри открытого блока: вложенных процедур в BSL нет — игнорируем.
        if _END_RE.match(raw):
            current["line_end"] = idx + 1
            blocks.append(current)
            current = None
    if current is not None:
        blocks.append(current)  # незакрытый блок: line_end остаётся None
    return blocks


def find_call_sites(text: str, name: str) -> list[dict]:
    """Строки собственных вызовов Имя( в модуле (не объявление, не Конец...).

    Возвращает [{line, fragment}] — по одному на строку, fragment без краевых
    пробелов. Имя сравнивается без учёта регистра (BSL); доступ через точку
    (ЧужойОбъект.Имя(...)) не считается — вызов другого объекта. Пустое/не
    идентификатор имя -> [].
    """
    if not _IDENT_RE.fullmatch(name):
        return []
    call_re = re.compile(
        r"(?<![A-Za-zА-Яа-яЁё0-9_.])" + re.escape(name) + r"\s*\(",
        re.IGNORECASE,
    )
    # Объявление того же имени в нестандартном регистре («ПРОЦЕДУРА Имя(...)»)
    decl_same = re.compile(
        r"^\s*(?:Процедура|Функция)\s+" + re.escape(name) + r"\b",
        re.IGNORECASE,
    )
    sites: list[dict] = []
    for idx, raw in enumerate(_normalize(text).split("\n"), 1):
        stripped = raw.strip()
        if stripped.startswith("//") or stripped.startswith("|"):
            continue  # комментарий или продолжение литерала — не вызов
        if _is_decl_line(raw) or decl_same.match(raw):
            continue  # объявление (в т.ч. одноимённое) — не вызов
        if _END_RE.match(raw):
            continue  # КонецПроцедуры/КонецФункции — не вызов
        if call_re.search(raw):
            sites.append({"line": idx, "fragment": stripped})
    return sites


if __name__ == "__main__":
    # Ручная проверка без платформы:
    #   python server/tools/module_structure.py <файл.bsl> [--name Имя] [--out файл.json]
    # Результат: {"blocks": [...], "calls": [...]} (calls — при --name).
    # Без --out печать в stdout с ensure_ascii=True (консоль Windows — CP866,
    # кириллица искажается; для читаемого вывода указывайте --out).
    _ap = argparse.ArgumentParser(description="Разбор структуры модуля 1С (BSL)")
    _ap.add_argument("file", help="текст модуля (UTF-8)")
    _ap.add_argument("--name", help="найти вызовы процедуры/функции по имени")
    _ap.add_argument("--out", help="файл результата в UTF-8 (по умолчанию stdout)")
    _ns = _ap.parse_args()
    _text = Path(_ns.file).read_text(encoding="utf-8-sig")
    _result: dict = {"blocks": parse(_text)}
    if _ns.name:
        _result["calls"] = find_call_sites(_text, _ns.name)
    _payload = json.dumps(_result, ensure_ascii=not _ns.out, indent=2)
    if _ns.out:
        Path(_ns.out).write_text(_payload + "\n", encoding="utf-8")
    else:
        print(_payload)
