# -*- coding: utf-8 -*-
"""Снимок-инвентарь конфигурации из контейнера cf в JSON — БЕЗ платформы (IDEAS №9).

Читает cf (формат cf_inspect, контейнер v2) и строит JSON-инвентарь:
объекты, сгруппированные по uuid, с записями (суффикс, размер, опционально
sha1), служебные записи посчитаны отдельно. Для диффов двух cf, отчётов и
триажа состава конфигурации.

Честное ограничение: структура РЕКВИЗИТОВ из cf недоступна — открытая проблема
резолвера (cf хранит сериализации без типа объекта: «{1,0,<uuid>},"Имя",» не
говорит, Справочник это или Документ). Инвентарь = объекты/записи/размеры/хеши.

Формат ответа (dump()):
  {
    "file": путь к cf, "version": версия-эвристика из root-записи (или null),
    "generated": iso-время UTC, "records_total": всего записей,
    "objects": [{"uuid", "name" (если распознано), "entries":
                 [{"suffix" (""/".0"/...), "size", "hash"?}]}],
    "service_entries": N, "note": честное ограничение
  }

Про "size" записи: поле size в заголовке блока — длина потока данных записи НА
ДИСКЕ (для deflate-блоков — сжатая длина; длина распакованного содержимого
больше, см. cf_inspect.payload). Мы НЕ распаковываем payload целиком: size уже
есть в заголовке — на cf в гигабайты дамп работает потоково (на каждую запись —
только peek первых байт для распознавания имени; полный read+sha1 только при
use_hash=True).

Имя объекта — «{1,0,<uuid>},"Имя",» в сериализации (канон 8.3.27, тот же подход,
что md_search_code.load_names / cf_recdiff._name_from_serialization): первые
байты записи распаковываются частично (_partial_text) и сканируются regex-ом.
Объекты, чьё имя не найдено ни в одной своей записи, отдаются без "name" —
это НЕ ошибка: сериализации могут не содержать пару имя-uuid (например,
.1-шаблоны пустых модулей).

CLI:
  python cf_dump_json.py --cf <файл> [--out <json>] [--hash] [--limit N]
  --out по умолчанию <cf>-dump.json (рядом с cf).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

UUID_RE = re.compile(r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                     r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:\.(\d+))?$")
NAME_RE = re.compile(r"\{1,0,([0-9a-f\-]{36})\},\"([^\"]+)\",")
VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+(?:\.\d+)?)\b")
NAME_SCAN_BYTES = 8192  # первые байты распакованной записи, где ищем имя
NOTE = ("инвентарь записей cf: объекты/записи/размеры/хеши — для диффов и "
        "отчётов; структура РЕКВИЗИТОВ из cf недоступна (открытая проблема "
        "резолвера: cf хранит сериализации без типа объекта)")


def _suffix(name: str) -> tuple[str, str] | None:
    """(uuid, суффикс ""/".N") для uuid-имени записи; None — не объект."""
    m = UUID_RE.match(name)
    if not m:
        return None
    return m.group(1).lower(), "." + m.group(2) if m.group(2) else ""


def _load_names(cf, records: list) -> dict:
    """{uuid_lower: имя} — частичное чтение первых байт каждой записи-объекта.

    Подход md_search_code.load_names, но потоковый: вместо cf.read (полная
    распаковка) — cf_inspect._partial_text (лимит байт) — на большом cf
    не читает модули целиком.
    """
    from cf_inspect import _partial_text  # noqa: E402
    names: dict = {}
    for r in records:
        if r["data_off"] is None:
            continue
        try:
            text = _partial_text(cf, r, NAME_SCAN_BYTES).decode(
                "utf-8-sig", errors="ignore")
        except Exception:
            continue
        for m in NAME_RE.finditer(text):
            names.setdefault(m.group(1).lower(), m.group(2))
    return names


def _version_from(cf, records: list) -> str | None:
    """Эвристика: версия N.N.N(.N) в payload-е root-записи.

    В канонических cf root содержит хеш ({2,<uuid>,<base64>}) и версии не
    имеет — тогда None (не ошибка). Ищем по записям, имя которых "root".
    """
    from cf_inspect import _partial_text  # noqa: E402
    for r in records:
        if r["name"] != "root" or r["data_off"] is None:
            continue
        try:
            text = _partial_text(cf, r, NAME_SCAN_BYTES).decode(
                "utf-8-sig", errors="ignore")
        except Exception:
            continue
        m = VERSION_RE.search(text)
        if m:
            return m.group(1)
    return None


def dump(cf_path: str, use_hash: bool = False, out_json: str = "") -> dict:
    """Инвентарь cf: см. докстринг модуля. При out_json — пишет JSON в файл.

    Потоково: полный read записи (распаковка) делается только для хеша;
    размеры — из заголовков, имена — из частичных распаковок (_partial_text).
    """
    from cf_inspect import CfFile  # noqa: E402
    if not cf_path:
        raise ValueError("путь к cf пуст")
    if not Path(cf_path).is_file():
        raise FileNotFoundError(cf_path)

    with CfFile(cf_path) as cf:
        records = cf.records(nested=True)
        objects_map: dict = {}
        service = 0
        for r in records:
            parsed = _suffix(r["name"])
            if parsed is None:
                service += 1
                continue
            uuid, suffix = parsed
            obj = objects_map.setdefault(uuid, {"uuid": uuid, "entries": []})
            obj["entries"].append({"suffix": suffix, "size": r["size"]})

        names = _load_names(cf, records)
        for uuid, obj in objects_map.items():
            if uuid in names:
                obj["name"] = names[uuid]

        if use_hash:
            # хеш — по каждой записи в порядке обхода: дубли (uuid, суффикс)
            # легальны (cf хранит копии записей), маппинг по счётчику
            idx = {u: 0 for u in objects_map}
            for r in records:
                parsed = _suffix(r["name"])
                if parsed is None:
                    continue
                uuid = parsed[0]
                entries = objects_map[uuid]["entries"]
                i = idx[uuid]
                if i < len(entries):
                    data = cf.read(r)
                    entries[i]["hash"] = hashlib.sha1(data).hexdigest()
                idx[uuid] += 1

        for obj in objects_map.values():
            obj["entries"].sort(key=lambda e: (
                int(e["suffix"][1:]) if e["suffix"] else -1, e["suffix"]))

        version = _version_from(cf, records)

    objects = []
    for uuid in sorted(objects_map,
                       key=lambda u: (objects_map[u].get("name") or u, u)):
        obj = objects_map[uuid]
        objects.append({
            "uuid": uuid,
            **({"name": obj["name"]} if "name" in obj else {}),
            "entries": obj["entries"],
        })

    res = {
        "file": str(Path(cf_path).resolve()),
        "version": version,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "records_total": len(records),
        "objects": objects,
        "service_entries": service,
        "note": NOTE,
    }
    if out_json:
        _write_json(res, out_json)
    return res


def _write_json(res: dict, path: str) -> None:
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


def _reconfigure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Снимок-инвентарь cf в JSON (без платформы 1С)")
    ap.add_argument("--cf", required=True, help="путь к cf-контейнеру")
    ap.add_argument("--out", default="",
                    help="файл JSON (по умолчанию <cf>-dump.json рядом с cf)")
    ap.add_argument("--hash", action="store_true",
                    help="посчитать sha1 payload-ов записей (полное чтение)")
    ap.add_argument("--limit", type=int, default=None,
                    help="ограничить число объектов в выводе")
    args = ap.parse_args(argv)

    try:
        out = args.out or (str(args.cf) + "-dump.json")
        res = dump(args.cf, use_hash=args.hash)
        if args.limit is not None:
            res["objects"] = res["objects"][:max(0, args.limit)]
        _write_json(res, out)
    except (ValueError, FileNotFoundError, OSError) as e:
        _reconfigure_stdout()
        print("cf_dump_json: %s" % e, file=sys.stderr)
        return 2

    _reconfigure_stdout()
    print("cf_dump_json: записей=%d, объектов=%d, служебных=%d, версия=%s"
          % (res["records_total"], len(res["objects"]),
             res["service_entries"], res["version"]))
    print("отчёт: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
