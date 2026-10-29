# -*- coding: utf-8 -*-
"""cf_diff — сравнение двух cf-конфигураций и классификация переносимости (этап 14).

Два слоя (план docs/stage-14-cf-diff-ext.md §3 ред. 2):

1. ПЕРВИЧНЫЙ ФИЛЬТР — записи контейнеров через cf_inspect: uuid + hash payload.
   Служебные записи (configinfo, version, versions, deleted, root, контейнерная
   заглушка) исключаются. Быстро, без выгрузок платформы (2 ГБ — минуты).

2. РАСКРЫТИЕ изменённых — частичные выгрузки /DumpConfigToFiles -listFile из ОБЕИХ
   cf (снимки развернуты mockup_init'ом в слоты). Обе выгрузки делает одна платформа —
   XML каноничен, сериализационная нормализация не нужна (ревью М-2).

3. КЛАССИФИКАЦИЯ по таблице переносимости (§1 ред. 2):
   изменённый модуль → interceptor; изменённая форма → not-transferable (form);
   реквизиты: добавленные → attrs-new, тип/удаление → not-transferable (attr);
   новый объект → not-transferable (new); удалённый → not-transferable (deleted).

Классификация работает по XML-выгрузкам раскрытых объектов; для не раскрытых
(только в одном cf) класс ставится по факту наличия.

Выход: diff.json-совместимый dict:
  {"new": [...], "deleted": [...], "changed": [{"name", "uuid", "cls", "classes": {...}}]}
и человекочитаемый отчёт.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent))

from cf_inspect import CfFile  # noqa: E402

SERVICE_RECORDS = re.compile(
    r"^(configinfo|version|versions|deleted|root)$|"
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RECORD_RE = re.compile(r"^([0-9a-f-]{36})(\.\d+)?$")
PLATFORM = r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe"


def _xml_sha(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def _full_xml_dump(base: str, out: Path, timeout: int = 3600) -> None:
    """Полная XML-выгрузка конфигурации слота (каноничный снимок платформы)."""
    out.mkdir(parents=True, exist_ok=True)
    res_file, log_file = out.parent / (out.name + ".res"), out.parent / (out.name + ".log")
    res_file.unlink(missing_ok=True)
    r = subprocess.run([PLATFORM, "DESIGNER", "/DisableStartupDialogs",
                        "/F", str(base), "/DumpConfigToFiles", str(out),
                        "/Out", str(log_file), "/DumpResult", str(res_file)],
                       capture_output=True, timeout=timeout)
    res = res_file.read_text(encoding="utf-8-sig", errors="replace").strip()         if res_file.is_file() else ""
    if r.returncode != 0 or res not in ("0", ""):
        raise RuntimeError(f"DumpConfigToFiles {out}: rc={r.returncode}, res={res!r}")


def compare_dumps(dir1: Path, dir2: Path) -> dict:
    """Слой классификации по ПОЛНЫМ XML-выгрузкам (канон платформы).
    КРИТИЧЕСКИЙ ФАКТ П14-4: хеши cf-записей НЕ стабильны между сохранениями
    (32 ложных changed на паре с 1 реальным изменением) — первичный слой
    сравнения по cf-записям УБРАН. Единственный надёжный diff — XML."""
    def collect(d: Path):
        files = {p.relative_to(d).as_posix(): p for p in d.rglob("*")
                 if p.is_file() and p.suffix in (".xml", ".bsl")
                 and p.name != "ConfigDumpInfo.xml"}
        info = d / "ConfigDumpInfo.xml"
        meta = {}
        if info.is_file():
            t = info.read_text(encoding="utf-8-sig", errors="replace")
            for m in re.finditer(r'MetaDataObject name="([^"]+)"[^>]*id="([0-9a-f-]{36})"', t):
                meta[m.group(1)] = m.group(2)
        return files, meta
    f1, m1 = collect(dir1)
    f2, m2 = collect(dir2)
    only1 = sorted(set(f1) - set(f2))
    only2 = sorted(set(f2) - set(f1))
    changed = []
    for k in sorted(set(f1) & set(f2)):
        if _xml_sha(f1[k]) != _xml_sha(f2[k]):
            entry = {"path": k, "classes": {}}
            stem = k.split("/")[-1].replace(".xml", "").replace(".bsl", "")
            # ConfigDumpInfo name="Catalog.X" — полное имя; сравниваем по хвосту
            entry["uuid"] = None
            for nm, u in {**m1, **m2}.items():
                if nm.split(".")[-1] == stem:
                    entry["uuid"] = u
                    entry["name"] = nm
                    break
            if k.endswith(".bsl"):
                entry["classes"]["module"] = True
            elif "/Forms/" in k:
                entry["classes"]["form"] = True
            else:
                t1 = f1[k].read_text(encoding="utf-8-sig", errors="replace")
                t2 = f2[k].read_text(encoding="utf-8-sig", errors="replace")
                a1 = set(re.findall(r'<Attribute uuid="([0-9a-f-]{36})"', t1))
                a2 = set(re.findall(r'<Attribute uuid="([0-9a-f-]{36})"', t2))
                if a2 - a1:
                    entry["classes"]["attrs-new"] = sorted(a2 - a1)
                if a1 - a2:
                    entry["classes"]["attr-removed"] = sorted(a1 - a2)
                if not any(v for v in entry["classes"].values()):
                    # Configuration.xml: списки ChildObjects сдвигаются при добавлении
                    # объекта — содержательное отличие только если изменились объекты,
                    # а их файлы уже посчитаны выше
                    entry["classes"]["config-list"] = True
            changed.append(entry)
    # новые объекты cf2: файлы только в cf2 (+ их owner-строка в Configuration.xml)
    new_objs = sorted({k.split("/")[1] for k in only2 if "/" in k})
    return {"changed": changed, "only1": only1, "only2": only2, "new_objs": new_objs}


def classify(full_diff: dict) -> dict:
    report = {"transfer": [], "manual": []}
    for e in full_diff["changed"]:
        for cls, val in e["classes"].items():
            if cls == "module":
                report["transfer"].append({"path": e["path"], "action": "interceptor"})
            elif cls == "attrs-new":
                report["transfer"].append({"path": e["path"], "action": "attrs-new",
                                           "attrs": val})
            else:
                report["manual"].append({"path": e["path"], "class": cls})
    for k in full_diff["only2"]:
        report["manual"].append({"path": k, "class": "new-file"})
    for k in full_diff["only1"]:
        report["manual"].append({"path": k, "class": "deleted-file"})
    return report


def run(cf1: str, cf2: str, work_dir: str) -> dict:
    """Полный конвейер (ред. 3): слоты → полные XML-выгрузки → сравнение → классификация."""
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    slots = {}
    for tag, cf in (("cf1", cf1), ("cf2", cf2)):
        slot = work / ("slot-" + tag)
        r = subprocess.run([sys.executable, str(TOOLS / "mockup_init.py"),
                            "--base", str(slot), "--from", cf, "--reset"],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(f"mockup_init {tag}: {r.stdout[-200:]} {r.stderr[-200:]}")
        slots[tag] = str(slot)
        print(f"слот {tag}: {round(time.time() - t0, 1)} с", flush=True)
    d1, d2 = work / "full-cf1", work / "full-cf2"
    _full_xml_dump(slots["cf1"], d1)
    print("выгрузка cf1:", round(time.time() - t0, 1), "с", flush=True)
    _full_xml_dump(slots["cf2"], d2)
    print("выгрузка cf2:", round(time.time() - t0, 1), "с", flush=True)
    fd = compare_dumps(d1, d2)
    report = {"cf1": cf1, "cf2": cf2, "full_diff": fd,
              "verdicts": classify(fd),
              "ms": round((time.time() - t0) * 1000)}
    (work / "diff.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="cf_diff: сравнение двух cf (этап 14, ред. 3)")
    ap.add_argument("cf1", help="вендорский эталон")
    ap.add_argument("cf2", help="доработанная конфигурация")
    ap.add_argument("--work", default=r"Y:\Snegopat-mcperify\cf_diff")
    ap.add_argument("--report", help="файл diff.json (по умолчанию work/diff.json)")
    a = ap.parse_args(argv)
    rep = run(a.cf1, a.cf2, a.work)
    fd = rep["full_diff"]
    print("изменённых файлов:", len(fd["changed"]),
          "| новых:", len(fd["only2"]), "| удалённых:", len(fd["only1"]))
    for v in rep["verdicts"]["transfer"]:
        print("  TRANSFER:", v["path"], "→", v["action"])
    for v in rep["verdicts"]["manual"]:
        print("  MANUAL:  ", v["path"], "→", v["class"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
