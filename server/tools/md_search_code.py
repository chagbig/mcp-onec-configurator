# -*- coding: utf-8 -*-
"""Поиск по текстам модулей конфигурации в cf — БЕЗ платформы (IDEAS №15).

Формат .0-сериализации (снят побайтово 10.11.2026 на probe-cf, см.
Y:\\Snegopat-mcp\\probe\\msc_*.py):

  blob записи = вложенный контейнер: 16-байтный заголовок, затем блоки
  «\\r\\n<size8> <page8> <mask8> \\r\\n» + region. Следующий блок стоит ровно на
  data_off + page (обход как cf_inspect.Container); region содержит size байт
  фактического содержимого, хвост региона — УСТАРЕВШИЙ мусор прежних сохранений
  (может включать копии старых заголовков страниц и старый текст модуля).

  Блок-«имя» (page=32: 16 байт timestamps + имя utf-16le «text\\0\\0\\0\\0\\0»)
  указывает на СЛЕДУЮЩИЙ блок — страницу исходного текста модуля; её
  фактическое содержимое = size байт (BOM + текст). Пустой модуль = size=3
  («\\ufeff»), старый текст при этом остаётся в хвосте региона — попадать в
  него нельзя, поэтому содержимое ограничивается полем size, а не границей
  региона. У нескольких блоков «text» в одной записи каждый даёт свою страницу
  (нумерация строк — внутри своей страницы).

  Строки модуля разделены «\\r\\r\\n» (квирк Конфигуратора) или «\\r\\n»;
  счёт строк и в том и в другом случае корректен по подстроке «\\r\\n»
  («\\r\\r\\n» содержит ровно одну «\\r\\n»).

  МОДУЛИ ФОРМ (проба 13.11.2026, forms_cf_probe5/6_20261113.py на probe-cf и
  demossl-A-noedits.cf): в .0-сериализации ФОРМЫ (запись «<uuid-формы>.0»)
  страниц text НЕТ (0 у всех 19 форм probe-cf), но текст модуля формы лежит
  внутри сериализации КАК QUOTED-СТРОКА: «,"#Область ОбработчикиСобытийФормы
  \\r\\n\\r\\n&НаСервере...#КонецОбласти",». Кавычки внутри удвоены («""»),
  разделители строк — обычные «\\r\\n» (crcrlf=0 во всех пробах). Такие строки
  ищутся тоже: содержимое достаётся между кавычками (удвоение снимается),
  line/col считаются по РАЗЭКРАНИРОВАННОМУ тексту; хит помечается src="form"
  (у страниц text — src="page"). Записи форм опознаются по маркеру скелета
  модуля; в ответе они получают form_record=true и form_title (заголовок
  формы из сериализации — синоним владельца, например «Ценные бумаги»).

Имя объекта — «{1,0,<uuid>},"Имя",» в сериализациях (канон 8.3.27, как в
cf_recdiff._name_from_serialization). Не найденные имена отдаются как короткий
uuid.

Поиск выполняется по СОХРАНЁННОЙ конфигурации: cf снимается cfg_archive_cf
(«Сохранить конфигурацию в файл» пишет последнее сохранённое состояние —
несохранённые правки модулей в cf не попадают; проверено 10.11: архив до
config_save не содержит module_set).

Замер на 2 ГБ cf (УТ): записи читаются ~6 с, поиск — needle-скан по страницам
(cf_recdiff — тот же подход).
"""
import argparse
import io
import json
import os
import re
import sys
from pathlib import Path

# CLI-запуск (в т.ч. python -I): сосед cf_inspect должен быть импортируемым
sys.path.insert(0, str(Path(__file__).resolve().parent))

TEXT_MARKER = b"t\x00e\x00x\x00t\x00\x00\x00\x00\x00"  # «text» utf-16le + 2 NUL
PAGE_HDR_RE = re.compile(rb"\r\n([0-9a-fA-F]{8}) ([0-9a-fA-F]{8}) ([0-9a-fA-F]{8}) \r\n")
NAME_RE = re.compile(r"\{1,0,([0-9a-f\-]{36})\},\"([^\"]+)\",")
SERVICE_RE = re.compile(r"^(root|version|versions|configinfo|deleted|\d+"
                        r"|[0-9a-f\-]{36}\.(?:1c|cfl|dc[a-z0-9]+))$", re.IGNORECASE)
BOM = b"\xef\xbb\xbf"
MAX_NAME_SCAN = 8192  # первые байты записи, где ищем {1,0,uuid},"Имя"
# скелет модуля формы, с которого начинается quoted-строка модуля в
# сериализации формы (см. докстринг модуля, проба 13.11.2026)
FORM_SKEL = "#Область ОбработчикиСобытийФормы"
FORM_STR_START = b'"' + FORM_SKEL.encode("utf-8")
FORM_TITLE_RE = re.compile(r'\{"ru","([^"]+)"\}')


def load_names(cf) -> dict:
    """{uuid_lower: имя} из сериализаций «{1,0,<uuid>},"Имя",» по всем записям."""
    names = {}
    for r in cf.records():
        data = cf.read(r)
        if not data:
            continue
        text = data[:MAX_NAME_SCAN].decode("utf-8", errors="ignore")
        for m in NAME_RE.finditer(text):
            names.setdefault(m.group(1).lower(), m.group(2))
    return names


def _text_pages(data: bytes) -> list:
    """[(start, end)] страниц исходного текста в blob записи.

    Обход блоков вложенного контейнера (cf_inspect.Container): блок-«имя»,
    оканчивающийся маркером «text» (utf-16le), указывает на следующий блок —
    страницу текста; её содержимое = size байт от data_off (хвост региона до
    data_off+page — устаревший мусор, в страницу не включается).
    """
    from cf_inspect import Container  # noqa: E402
    pages = []
    try:
        cont = Container(io.BytesIO(data), 16)
        blocks = list(cont.walk())
    except Exception:
        return pages
    for k, blk in enumerate(blocks):
        off = blk["data_off"]
        region = data[off: off + blk["page"]]
        if not region.endswith(TEXT_MARKER):
            continue
        if k + 1 >= len(blocks):
            continue
        nxt = blocks[k + 1]
        start = nxt["data_off"]
        end = min(start + nxt["size"], start + nxt["page"], len(data))
        if end > start:
            pages.append((start, end))
    return pages


def _form_module_texts(data: bytes) -> list:
    """Тексты модулей форм в blob записи: quoted-строки, начинающиеся со
    скелета «#Область ОбработчикиСобытийФормы» (см. докстринг модуля).

    Маркер должен начинать строку (перед ним кавычка); конец строки —
    закрывающая кавычка, не входящая в удвоение «""». Возвращается
    разэкранированный текст (кавычки «""» → «"») — line/col по нему
    соответствуют настоящему модулю формы.
    """
    texts = []
    i = 0
    while True:
        i = data.find(FORM_STR_START, i)
        if i == -1:
            break
        if i == 0 or data[i - 1:i] != b'"':  # «"»+маркер — открытие строки,
            # а не вторая кавычка удвоения «""#Область…» внутри чужой строки
            s = i + 1  # содержимое после открывающей кавычки
            j = s
            end = -1
            while True:
                k = data.find(b'"', j)
                if k == -1:
                    break
                if data[k + 1: k + 2] == b'"':
                    j = k + 2  # удвоенная кавычка внутри строки
                    continue
                end = k
                break
            if end != -1:
                texts.append(data[s:end].replace(b'""', b'"')
                             .decode("utf-8", errors="ignore"))
                i = end + 1
                continue
        i += 1
    return texts


def _hits_in_text(text: str, src: str, needle: str, use_regex: bool,
                  rx, max_hits: int, collected: list) -> None:
    """Дописывает хиты одного текста (страница text или модуль формы) в
    collected: [{line, col, fragment, src}], не больше max_hits всего."""
    if use_regex:
        matches = rx.finditer(text)
    else:
        matches = re.finditer(re.escape(needle), text, re.IGNORECASE)
    for m in matches:
        if len(collected) >= max_hits:
            break
        pos = m.start()
        line = text.count("\r\n", 0, pos) + 1
        nl = text.rfind("\n", 0, pos)
        col = pos - (nl + 1)
        frag = text[max(0, pos - 60): pos + len(m.group(0)) + 60]
        frag = frag.replace("\r\r\n", " ⏎ ").replace("\r\n", " ⏎ ").replace("\x00", "")
        collected.append({"line": line, "col": col, "fragment": frag.strip(),
                          "src": src})


def search_record(data: bytes, needle: str, use_regex: bool,
                  max_hits: int) -> list:
    """Хиты в одной записи: [{"line", "col", "fragment", "src"}].

    Сканируются (1) страницы text (.0-модульный текст: МодульОбъекта/
    Менеджера/Общего модуля) и (2) quoted-строки модулей форм в сериализациях
    форм (src="form", разэкранированный текст, разделители «\\r\\n»).
    Попадания за пределами этих текстов (info-страницы, устаревшие хвосты,
    служебные блоки) невозможны по построению.
    """
    texts = []  # [(text, src)]
    for start, end in _text_pages(data):
        content = data[start:end]
        if content.startswith(BOM):
            content = content[len(BOM):]
        texts.append((content.decode("utf-8", errors="ignore"), "page"))
    for fm in _form_module_texts(data):
        texts.append((fm, "form"))
    if not texts:
        return []
    rx = None
    if use_regex:
        try:
            rx = re.compile(needle, re.IGNORECASE | re.MULTILINE)
        except re.error:
            return []
    hits = []
    for text, src in texts:
        _hits_in_text(text, src, needle, use_regex, rx, max_hits, hits)
        if len(hits) >= max_hits:
            break
    return hits


def is_form_record(data: bytes) -> bool:
    """Запись — сериализация формы (есть quoted-строка модуля формы)."""
    return FORM_STR_START in data


def form_title(data: bytes) -> str | None:
    """Заголовок формы из сериализации (синоним владельца) или None."""
    m = FORM_TITLE_RE.search(data[:3000].decode("utf-8", errors="ignore"))
    return m.group(1) if m else None


def search(cf_path: str, query: str, use_regex: bool = False,
           limit: int = 50, max_hits_per_object: int = 20) -> dict:
    from cf_inspect import CfFile  # noqa: E402
    if use_regex:
        try:
            re.compile(query, re.IGNORECASE)
        except re.error as e:
            raise ValueError("неверный regex: %s" % e)
    if not os.path.isfile(cf_path):
        raise FileNotFoundError(cf_path)

    results = []
    objects_scanned = 0
    with CfFile(cf_path) as cf:
        names = load_names(cf)
        for r in cf.records():
            name = r["name"]
            if not name.endswith(".0") or SERVICE_RE.match(name[:-2]):
                continue
            objects_scanned += 1
            data = cf.read(r)
            if not data:
                continue
            hits = search_record(data, query, use_regex, max_hits_per_object)
            if not hits:
                continue
            uuid = name[:-2].lower()
            obj = names.get(uuid)
            entry = {
                "record": name,
                "uuid": uuid,
                "object": obj or (uuid[:8] + "…"),
                "object_known": obj is not None,
                "hits": hits,
            }
            if is_form_record(data):  # сериализация формы, не модуль объекта
                entry["form_record"] = True
                entry["form_title"] = form_title(data)
            results.append(entry)
            if sum(len(x["hits"]) for x in results) >= limit:
                break
    return {"query": query, "regex": use_regex, "cf": os.path.abspath(cf_path),
            "objects_scanned": objects_scanned, "matches": results,
            "total_hits": sum(len(x["hits"]) for x in results),
            "note": "поиск по сохранённой конфигурации (cf от cfg_archive_cf); "
                    "страницы text .0-сериализаций (src=\"page\") и модули форм "
                    "— quoted-строки сериализаций форм, разэкранированные "
                    "(src=\"form\"); содержимое ограничено полем size страницы "
                    "(устаревшие хвосты исключены)"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Поиск по текстам модулей конфигурации в cf (без платформы)")
    ap.add_argument("--cf", required=True, help="путь к cf (cfg_archive_cf)")
    ap.add_argument("query", help="подстрока или regex (--regex)")
    ap.add_argument("--regex", action="store_true", help="query — регулярное выражение")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--out", help="путь JSON-вывода (по умолчанию stdout)")
    args = ap.parse_args(argv)

    try:
        res = search(args.cf, args.query, args.regex, args.limit)
    except (ValueError, FileNotFoundError) as e:
        print("md_search_code: %s" % e, file=sys.stderr)
        return 2
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if args.out:
        io.open(args.out, "w", encoding="utf-8").write(text)
    else:
        for stream in (sys.stdout,):
            try:
                if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
                    stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
