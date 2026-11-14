# -*- coding: utf-8 -*-
"""cf_recdiff — записный скан двух cf-контейнеров (быстрый триаж, v1.3).

За 1–2 минуты на 2 ГБ паре выдаёт: новые/удалённые/изменённые записи контейнера,
рекреированные записи (обнулена в одной cf, контент под другим uuid в другой),
имена объектов из собственных сериализаций. Это ТРИАЖ-отчёт «отличаются ли cf
и чем примерно» — финальную семантику снимает cf_diff (XML-канон).

CLI:
  python cf_recdiff.py --cf1 <файл> --cf2 <файл> [--json <файл>]

Ограничения (v1.3): имени вида (Документ/Справочник) в записях нет — только
имя объекта из своей сериализации; служебные записи root/version/versions/
configinfo/deleted исключены; configVersion-строки в root — шум, отмечен отдельно.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _service(name: str) -> bool:
    return name in ("root", "version", "versions", "configinfo", "deleted")


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _name_from_serialization(text: str, rec_uuid: str) -> str | None:
    """Имя объекта из своей сериализации: {1,0,<uuid>,"Имя", — канон 8.3.27."""
    m = re.search(r'\{1,0,' + re.escape(rec_uuid) + r'\},"([^"]+)",', text)
    return m.group(1) if m else None


def scan(cf1_path: str, cf2_path: str, max_changed_detail: int = 40) -> dict:
    from cf_inspect import CfFile  # noqa: E402

    def read_all(path: str) -> tuple[dict, dict]:
        """(Counter пар (имя, хеш)), {имя: размер} — Counter НЕ схлопывает дубли имён."""
        sizes = {}
        pairs = Counter()
        with CfFile(path) as cf:
            for r in cf.records():
                n = r["name"]
                if _service(n):
                    continue
                d = cf.read(r)
                sizes[n] = len(d)
                pairs[(n, _sha(d))] += 1
        return pairs, sizes

    p1, s1 = read_all(cf1_path)
    p2, s2 = read_all(cf2_path)

    names1 = {k[0] for k in p1}
    names2 = {k[0] for k in p2}
    # классификация ПО ИМЕНИ записи: new/del — имя только в одной cf;
    # changed — имя в обеих, но хеши (или число копий) различаются
    new_names = sorted(names2 - names1)
    del_names = sorted(names1 - names2)
    ch_names = []
    for n in sorted(names1 & names2):
        h1 = {k[1] for k in p1 if k[0] == n}
        h2 = {k[1] for k in p2 if k[0] == n}
        if h1 != h2 or sum(1 for k in p1 if k[0] == n) != sum(1 for k in p2 if k[0] == n):
            ch_names.append(n)

    # рекреированные: в одной cf запись пустая, в другой с контентом
    recreated = []
    with CfFile(cf1_path) as f1, CfFile(cf2_path) as f2:
        r1 = {r["name"]: r for r in f1.records()}
        r2 = {r["name"]: r for r in f2.records()}
        detail = []
        for n in ch_names:
            if n not in r1 or n not in r2:
                continue
            d1 = f1.read(r1[n])
            d2 = f2.read(r2[n])
            if not d1 and d2:
                recreated.append({"record": n, "side": "cf2", "size": len(d2)})
            elif not d2 and d1:
                recreated.append({"record": n, "side": "cf1", "size": len(d1)})
        # имена новых/изменённых: из своей сериализации; у записей с суффиксом
        # (.0/.2) имя берём из sibling-записи без суффикса (обёртка с именем)
        named = []
        for n in (new_names[:max_changed_detail] + ch_names[:max_changed_detail]):
            base = n.replace(".0", "").replace(".2", "")
            nm = None
            for src, rr in ((f2, r2), (f1, r1)):
                if n in rr:
                    txt = src.read(rr[n]).decode("utf-8", errors="replace")
                    nm = _name_from_serialization(txt, base)
                    if nm:
                        break
                    # sibling-обёртка того же объекта в той же cf
                    if base != n and base in rr:
                        wtxt = src.read(rr[base]).decode("utf-8", errors="replace")
                        nm = _name_from_serialization(wtxt, base)
                        if nm:
                            break
            named.append({"record": n, "name": nm})
    return {
        "cf1": cf1_path, "cf2": cf2_path,
        "records1": len(names1), "records2": len(names2),
        "new": new_names, "deleted": del_names, "changed": ch_names,
        "recreated": recreated[:20],
        "named": named,
    }


def main(argv=None) -> int:
    # stdout может быть cp1252 (CI-раннер) — кириллица в print роняет процесс
    # (UnicodeEncodeError) → v1.10.0 CI. Ребёнок оркестратора обязан писать utf-8
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Записный скан двух cf (быстрый триаж)")
    ap.add_argument("--cf1", required=True)
    ap.add_argument("--cf2", required=True)
    ap.add_argument("--json", default="", help="файл отчёта JSON")
    a = ap.parse_args(argv)
    res = scan(a.cf1, a.cf2)
    print(f"записей: {res['records1']} vs {res['records2']}")
    print(f"новых: {len(res['new'])}, удалённых: {len(res['deleted'])}, "
          f"изменённых: {len(res['changed'])}, рекреированных: {len(res['recreated'])}")
    for n in res["new"]:
        print("  NEW ", n)
    for n in res["deleted"]:
        print("  DEL ", n)
    for nm in res["named"]:
        if nm["name"]:
            print(f"  имя [{nm['record'][:20]}]: {nm['name']}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        print("отчёт:", a.json)
    interesting = bool(res["new"] or res["deleted"] or res["changed"])
    return 0 if not interesting else 2


if __name__ == "__main__":
    sys.exit(main())
