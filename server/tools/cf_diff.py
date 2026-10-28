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


def record_inventory(cf_path: str) -> dict:
    """{uuid: {"root": sha1 записи, "parts": {".0": sha1, ".2": sha1, ...}}}."""
    inv = {}
    with CfFile(cf_path) as cf:
        for r in cf.records():
            m = RECORD_RE.match(r["name"])
            if not m:
                continue  # служебные имена (root, version, configinfo...)
            uuid, part = m.group(1), m.group(2) or ""
            h = hashlib.sha1(cf.read(r)).hexdigest()
            if part == "":
                inv.setdefault(uuid, {"root": h, "parts": {}})
            else:
                inv.setdefault(uuid, {"root": "", "parts": {}})["parts"][part] = h
    return inv


def diff_inventory(inv1: dict, inv2: dict) -> dict:
    """Слой 1: {'new': [uuid], 'deleted': [uuid], 'changed': [uuid]}"""
    k1, k2 = set(inv1), set(inv2)
    changed = []
    for u in k1 & k2:
        if inv1[u] != inv2[u]:
            changed.append(u)
    return {"new": sorted(k2 - k1), "deleted": sorted(k1 - k2), "changed": sorted(changed)}


def dump_objects(base: str, objects: list, work_dir: Path,
                 timeout: int = 1800) -> Path:
    """Частичная выгрузка /DumpConfigToFiles -listFile (слой 2). objects — имена
    вида «Справочник.X» (верхний уровень; подчинённые едут с ними). Возвращает dir."""
    work_dir.mkdir(parents=True, exist_ok=True)
    lf = work_dir / "dump.list"
    lf.write_text("\n".join(objects) + "\n", encoding="utf-8")
    out = work_dir / "xml"
    res_file, log_file = work_dir / "dump.res", work_dir / "dump.log"
    res_file.unlink(missing_ok=True)
    r = subprocess.run([PLATFORM, "DESIGNER", "/DisableStartupDialogs",
                        "/F", str(base), "/DumpConfigToFiles", str(out),
                        "-listFile", str(lf),
                        "/Out", str(log_file), "/DumpResult", str(res_file)],
                       capture_output=True, timeout=timeout)
    res = res_file.read_text(encoding="utf-8-sig", errors="replace").strip() \
        if res_file.is_file() else ""
    if r.returncode != 0 or res not in ("0", ""):
        raise RuntimeError(f"DumpConfigToFiles: rc={r.returncode}, res={res!r}")
    return out


def _xml_sha(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def compare_dumps(dir1: Path, dir2: Path, uuid_index: dict) -> list:
    """Слой 3: сравнение XML-выгрузок раскрытых объектов.
    ConfigDumpInfo.xml исключается (служебный). Возвращает список
    {'name', 'cls', 'uuid', 'classes': {...}}."""
    result = []
    def collect(d: Path):
        files = {p.relative_to(d).as_posix(): p for p in d.rglob("*.xml")
                 if p.name != "ConfigDumpInfo.xml"}
        info = d / "ConfigDumpInfo.xml"
        meta = {}
        if info.is_file():
            t = info.read_text(encoding="utf-8-sig", errors="replace")
            for m in re.finditer(r'<MetaDataObject name="([^"]+)"[^>]*id="([0-9a-f-]{36})"[^>]*>', t):
                pass
            # формат: <ConfigDumpInfo>...<MetaDataObject name="Справочник.X" id="uuid" ...>
            for m in re.finditer(r'MetaDataObject name="([^"]+)"[^>]*id="([0-9a-f-]{36})"', t):
                meta[m.group(2)] = m.group(1)
        return files, meta
    files1, meta1 = collect(dir1)
    files2, meta2 = collect(dir2)
    umeta = {**meta1, **meta2}
    for key in sorted(set(files1) & set(files2)):
        p1, p2 = files1[key], files2[key]
        if _xml_sha(p1) == _xml_sha(p2):
            continue
        t1 = p1.read_text(encoding="utf-8-sig", errors="replace")
        t2 = p2.read_text(encoding="utf-8-sig", errors="replace")
        entry = {"path": key, "classes": {}, "detail": {}}
        # модули: Module.bsl рядом (Ext/ObjectModule.bsl, Ext/ManagerModule.bsl, Ext/Form/Module.bsl)
        for mod_rel in ("Ext/ObjectModule.bsl", "Ext/ManagerModule.bsl",
                        "Ext/Form/Module.bsl", "Ext/RecordSetModule.bsl"):
            m1, m2 = p1.parent / mod_rel, p2.parent / mod_rel
            if m1.is_file() or m2.is_file():
                if not (m1.is_file() and m2.is_file() and
                        m1.read_bytes() == m2.read_bytes()):
                    entry["classes"]["module"] = mod_rel
        # реквизиты: сравнение <Attribute uuid= в дочерних XML объекта (конфигурационный xml)
        attrs1 = set(re.findall(r'<Attribute uuid="([0-9a-f-]{36})"', t1))
        attrs2 = set(re.findall(r'<Attribute uuid="([0-9a-f-]{36})"', t2))
        added = attrs2 - attrs1
        removed = attrs1 - attrs2
        if added:
            entry["classes"]["attrs-new"] = sorted(added)
        if removed:
            entry["classes"]["attr-removed"] = sorted(removed)
        # изменение типа существующего реквизита: тот же uuid, другой Type-блок
        for au in attrs1 & attrs2:
            bt1 = _attr_type(t1, au)
            bt2 = _attr_type(t2, au)
            if bt1 != bt2:
                entry["classes"]["attr-type-changed"] = entry["classes"].get(
                    "attr-type-changed", []) + [au]
        # формы: Forms/ каталог различается
        f1 = {q.name for q in p1.parent.rglob("Forms/*/**")}
        f2 = {q.name for q in p2.parent.rglob("Forms/*/**")}
        # упрощённо: сравнение Form.xml sha
        forms1 = {q.parent.name: _xml_sha(q) for q in p1.parent.rglob("Forms/*/Ext/Form.xml")}
        forms2 = {q.parent.name: _xml_sha(q) for q in p2.parent.rglob("Forms/*/Ext/Form.xml")}
        if forms1 != forms2:
            entry["classes"]["form"] = sorted(set(forms1) | set(forms2))
        if not entry["classes"]:
            # XML отличается, но классифицировать нечем — признак служебного изменения
            entry["classes"]["other"] = True
        # имя/класс/uuid из ConfigDumpInfo выгрузки (надёжный источник)
        stem = key.split("/")[-1].replace(".xml", "")
        entry["name"] = stem
        entry["uuid"] = None
        for u, nm in umeta.items():
            if nm == stem or nm.endswith("." + stem) or nm == stem.split(".")[0]:
                entry["uuid"] = u
                entry["name"] = nm
                break
        result.append(entry)
    # новые/удалённые объекты по каталогам выгрузки
    return result


def _attr_type(xml_text: str, attr_uuid: str) -> str:
    m = re.search(r'<Attribute uuid="%s">.*?<Type>(.*?)</Type>' % attr_uuid,
                  xml_text, re.S)
    return re.sub(r"\s+", "", m.group(1)) if m else ""


def classify(dump_diff: list) -> dict:
    """Таблица переносимости (§1 ред. 2): класс → вердикт."""
    verdicts = []
    for e in dump_diff:
        transfer, manual = [], []
        for cls in e["classes"]:
            if cls == "module":
                transfer.append("interceptor:" + e["classes"][cls])
            elif cls == "attrs-new":
                transfer.append("attrs-new")
            elif cls in ("attr-removed", "attr-type-changed"):
                manual.append("not-transferable:" + cls)
            elif cls == "form":
                manual.append("not-transferable:form")
            else:
                manual.append("unknown:" + cls)
        verdicts.append({"name": e.get("name"), "uuid": e.get("uuid"), "transfer": transfer, "manual": manual})
    return verdicts


def run(cf1: str, cf2: str, work_dir: str, bases: dict | None = None) -> dict:
    """Полный diff-конвейер. bases — {'cf1': слот-база, 'cf2': слот-база} для
    раскрытия; если не заданы — слоты создаются через mockup_init."""
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    inv1 = record_inventory(cf1)
    inv2 = record_inventory(cf2)
    layer1 = diff_inventory(inv1, inv2)
    report = {"cf1": cf1, "cf2": cf2, "filter": layer1,
              "filter_ms": round((time.time() - t0) * 1000)}
    changed = layer1["changed"]
    if not changed:
        report["verdicts"] = []
        report["note"] = "изменённых записей нет"
        return report
    # слоты для раскрытия
    bases = bases or {}
    slots = {}
    import uuid as _u
    for tag, cf in (("cf1", cf1), ("cf2", cf2)):
        if bases.get(tag):
            slots[tag] = bases[tag]
        else:
            slot = work / ("slot-" + tag)
            r = subprocess.run([sys.executable, str(TOOLS / "mockup_init.py"),
                                "--base", str(slot), "--from", cf, "--reset"],
                               capture_output=True, text=True, timeout=3600)
            if r.returncode != 0:
                raise RuntimeError(f"mockup_init {tag}: {r.stdout[-200:]} {r.stderr[-200:]}")
            slots[tag] = str(slot)
    # имена объектов по uuid — из корневого дерева cf (uuid → имя не тривиально),
    # поэтому -listFile составляем по ВЫГРУЗКЕ-КАРТЕ:DumpConfigToFiles без -listFile
    # для изменённых uuid нельзя — нужны имена. Получаем имена из десериализации:
    # uuid запись <uuid> (без части) — сериализация объекта содержит {3,{1,0,<uuid>},"Имя"
    name_map = {}
    with CfFile(cf1 if cf1 else cf2) as _cf:
        pass
    for tag, cf in (("cf1", cf1), ("cf2", cf2)):
        with CfFile(cf) as c:
            recs = {r["name"]: r for r in c.records()}
            for u in changed:
                if u in name_map:
                    continue
                # имя ищем в записи <uuid> обоих cf
                for src_cf in (cf1, cf2):
                    try:
                        with CfFile(src_cf) as c2:
                            rr = {r["name"]: r for r in c2.records()}
                        for cand in (u, u + ".0"):
                            if cand in rr:
                                t = c2.read(rr[cand]).decode("utf-8-sig", errors="replace")
                                m = re.search(r'\{1,0,%s\},"([^"]+)"' % u, t)
                                if m:
                                    name_map[u] = m.group(1)
                                break
                    except Exception:
                        continue
                if u in name_map:
                    break
    # uuid→имя найдено не для всех: неотображённые — в отчёт как unresolved
    named = sorted({name_map[u] for u in changed if u in name_map})
    report["unresolved"] = [u for u in changed if u not in name_map]
    d1 = dump_objects(slots["cf1"], named, work / "dump-cf1")
    d2 = dump_objects(slots["cf2"], named, work / "dump-cf2")
    dd = compare_dumps(d1, d2, name_map)
    report["dump_diff"] = dd
    report["verdicts"] = classify(dd)
    report["ms"] = round((time.time() - t0) * 1000)
    (work / "diff.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="cf_diff: сравнение двух cf (этап 14)")
    ap.add_argument("cf1", help="вендорский эталон")
    ap.add_argument("cf2", help="доработанная конфигурация")
    ap.add_argument("--work", default=r"Y:\Snegopat-mcp\verify\cf_diff")
    ap.add_argument("--report", help="файл для diff.json (по умолчанию work/diff.json)")
    a = ap.parse_args(argv)
    rep = run(a.cf1, a.cf2, a.work)
    out = a.report or (str(Path(a.work) / "diff.json"))
    Path(out).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print("new:", len(rep["filter"]["new"]), "| deleted:", len(rep["filter"]["deleted"]),
          "| changed:", len(rep["filter"]["changed"]))
    for v in rep.get("verdicts", []):
        print(" ", v.get("name"), "| transfer:", v["transfer"], "| manual:", v["manual"])
    if rep.get("unresolved"):
        print("unresolved uuid:", rep["unresolved"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
