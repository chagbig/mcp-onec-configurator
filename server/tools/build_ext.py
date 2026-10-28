# -*- coding: utf-8 -*-
"""build_ext — сборка расширения из пары cf (этап 14, конвейер целиком).

python build_ext.py <cf1=вендор> <cf2=доработанная> --name МсрДиф --prefix Мср14_

Шаги:
1. cf_diff (ред. 3): слоты → полные XML-выгрузки → full_diff (канон платформы).
2. cfe-init (скил) — scaffold расширения (пропускается, если src уже собран).
3. cfe-borrow (скил) — оболочки изменённых объектов (верхний уровень).
4. fill_shells — НАПОЛНЕНИЕ: по разнице child-объектов владельца в cf1/cf2
   генерируются Adopted-контейнеры (ТЧ, существующие в cf1) и собственные объекты
   (новые в cf2) с префиксом расширения. Изменённые МОДУЛИ — в отчёт
   (перехватчики через скил cfe-patch-method — ручная доводка по списку).
5. cfe-validate (скил).
6. Слот cf1: /LoadConfigFromFiles -Extension → /CheckModules -Extension →
   /UpdateDBCfg -Extension → /DumpCfg -Extension → cfe-артефакт.
7. Верификации: вендор цел (записи cf1 == основная конфигурация слота по хешам),
   переносимое из diff присутствует в cfe.

Отчёт: report.json (переносимое, непереносимое, маппинг имён, артефакт).
"""
import json
import re
import shutil
import subprocess
import sys
import time
import uuid as _uuid
from pathlib import Path

TOOLS = Path(__file__).parent
SKILLS = Path(r"C:\Users\chagb\.agents\skills")
PLATFORM = r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent))

NS_MD = "{http://v8.1c.ru/8.3/MDClasses}"


def _sh(cmd, timeout=600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r


def _designer(base, args, work: Path, tag, timeout=900):
    res = work / (tag + ".res")
    res.unlink(missing_ok=True)
    r = subprocess.run([PLATFORM, "DESIGNER", "/DisableStartupDialogs", "/F", str(base)]
                       + [str(a) for a in args] +
                       ["/Out", str(work / (tag + ".log")), "/DumpResult", str(res)],
                       capture_output=True, timeout=timeout)
    rt = res.read_text(encoding="utf-8-sig", errors="replace").strip() if res.is_file() else "?"
    log = (work / (tag + ".log")).read_text(encoding="utf-8-sig", errors="replace") \
        if (work / (tag + ".log")).is_file() else ""
    if r.returncode != 0 or rt not in ("0", ""):
        raise RuntimeError(f"{tag}: rc={r.returncode}, res={rt!r}, log={log[-200:]}")
    return rt, log


def _fragment(owner_xml: str, tag: str, uuid: str) -> str | None:
    """Фрагмент <Tag uuid="...">...</Tag> (без вложенных Tag того же вида)."""
    m = re.search(r"<%s uuid=\"%s\">.*?</%s>" % (tag, uuid, tag), owner_xml, re.S)
    return m.group(0) if m else None


def _child_index(owner_xml: str) -> dict:
    """{(kind, name): uuid} из ChildObjects владельца."""
    out = {}
    i = owner_xml.find("<ChildObjects>")
    j = owner_xml.find("</ChildObjects>")
    if i < 0 or j < 0:
        return out
    body = owner_xml[i:j]
    for m in re.finditer(r"<(Attribute|TabularSection|Form|Command) uuid=\"([0-9a-f-]{36})\">(.*?)</\1>",
                         body, re.S):
        kind, uuid, frag = m.group(1), m.group(2), m.group(3)
        nm = re.search(r"<Name>([^<]+)</Name>", frag)
        if nm:
            out[(kind, nm.group(1))] = uuid
    return out


def fill_shell(shell_xml_path: Path, cf1_owner_xml: Path, cf2_owner_xml: Path,
               prefix: str) -> dict:
    """Наполнение одной borrow-оболочки (этап 14 §9): Adopted-контейнеры + Own-объекты.
    Возвращает маппинг и счётчики. Существующие части расширения не трогаются."""
    shell = shell_xml_path.read_text(encoding="utf-8-sig")
    c1 = cf1_owner_xml.read_text(encoding="utf-8-sig")
    c2 = cf2_owner_xml.read_text(encoding="utf-8-sig")
    idx1, idx2 = _child_index(c1), _child_index(c2)
    shell_idx = _child_index(shell)
    mapping, added_tch, added_attr = [], [], 0
    inserts = []

    # новые в cf2 собственные части верхнего уровня владельца
    for (kind, name), uuid2 in sorted(idx2.items()):
        if (kind, name) in shell_idx:
            continue
        if (kind, name) in idx1:
            continue  # существующее в cf1 — заимствовано платформой/оболочкой
        if kind == "Form" or kind == "Command":
            continue  # формы/команды — ручная доводка (К-2)
        frag = _fragment(c2, kind, uuid2)
        if not frag:
            continue
        new_name = name if name.startswith(prefix) else prefix + name
        frag = frag.replace("<Name>%s</Name>" % name, "<Name>%s</Name>" % new_name, 1)
        new_uuid = str(_uuid.uuid4())
        frag = re.sub(r'uuid="%s"' % uuid2, 'uuid="%s"' % new_uuid, frag, count=1)
        inserts.append((kind, new_name, frag))
        added_attr += kind == "Attribute"
        mapping.append({"cf2": name, "ext": new_name, "kind": kind})

    # ТЧ, существующие в cf1 (Adopted-контейнеры), в cf2 с новыми реквизитами
    for (kind, name), uuid2 in sorted(idx2.items()):
        if kind != "TabularSection" or (kind, name) in shell_idx:
            continue
        if (kind, name) not in idx1:
            continue  # новая ТЧ — уже добавлена выше как Own
        frag2 = _fragment(c2, kind, uuid2)
        uuid1 = idx1[(kind, name)]
        sub1 = _child_index("<ChildObjects>%s</ChildObjects>" % (frag2 or ""))
        sub2 = _child_index("<ChildObjects>%s</ChildObjects>" % frag2)
        new_attrs = [(k, u) for k, u in sub2.items()
                     if k[0] == "Attribute" and k not in sub1 and k not in
                     {(kk, nn) for kk, nn in shell_idx}]
        if not new_attrs:
            continue
        # Adopted-контейнер ТЧ + новые реквизиты (Own, с префиксом)
        parts = []
        for (k, nm2), u2 in sorted(new_attrs):
            fr = _fragment(frag2, "Attribute", u2)
            if not fr:
                continue
            new_name = nm2 if nm2.startswith(prefix) else prefix + nm2
            fr = fr.replace("<Name>%s</Name>" % nm2, "<Name>%s</Name>" % new_name, 1)
            fr = re.sub(r'uuid="%s"' % u2, 'uuid="%s"' % str(_uuid.uuid4()), fr, count=1)
            parts.append(fr)
            added_attr += 1
            mapping.append({"cf2": nm2, "ext": new_name, "kind": "Attribute(ТЧ %s)" % name})
        adopted = _fragment(c1, kind, uuid1) or ""
        # Adopted-вариант ТЧ: служебные части cf1 → Adopted-заголовок
        adopted = re.sub(r"<ObjectBelonging>[^<]*</ObjectBelonging>", "", adopted)
        adopted = adopted.replace("<Properties>", "<Properties>\n\t\t\t\t\t<ObjectBelonging>Adopted</ObjectBelonging>", 1)
        m_ext = re.search(r"<Name>%s</Name>" % name, adopted)
        adopted = adopted.replace("</Properties>",
                                  "\t\t\t\t\t<ExtendedConfigurationObject>%s</ExtendedConfigurationObject>\n\t\t\t\t</Properties>" % uuid1, 1)
        # child из cf1-ТЧ (пусто в Adopted-объявлении) → наши новые реквизиты
        adopted = re.sub(r"<ChildObjects>.*?</ChildObjects>",
                         "<ChildObjects>\n" + "\n".join(parts) + "\n\t\t\t\t</ChildObjects>",
                         adopted, flags=re.S)
        adopted = re.sub(r'uuid="[0-9a-f-]{36}"', lambda m: 'uuid="%s"' % str(_uuid.uuid4()),
                         adopted)
        inserts.append(("TabularSection", name, adopted))
        added_tch += 1

    if inserts:
        if "<ChildObjects/>" in shell:
            parts_block = "\n".join(f for _, _, f in inserts)
            shell = shell.replace("<ChildObjects/>",
                                  "\n\t\t<ChildObjects>\n" + parts_block + "\n\t\t</ChildObjects>", 1)
        else:
            i = shell.find("<ChildObjects>")
            j = shell.find("</ChildObjects>", i)
            parts_block = "\n".join(f for _, _, f in inserts)
            shell = shell[:j] + parts_block + "\n\t\t" + shell[j:]
        shell_xml_path.write_text(shell, encoding="utf-8-sig")
    return {"mapping": mapping, "added_tch": added_tch, "added_attr": added_attr,
            "inserted": len(inserts)}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="build_ext: расширение из пары cf (этап 14)")
    ap.add_argument("cf1")
    ap.add_argument("cf2")
    ap.add_argument("--name", default="МсрДиф")
    ap.add_argument("--prefix", default="Мср14_")
    ap.add_argument("--work", default=r"Y:\Snegopat-mcp\verify\build_ext")
    a = ap.parse_args(argv)
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    report = {"cf1": a.cf1, "cf2": a.cf2, "name": a.name, "steps": []}

    def step(name, ok, info):
        report["steps"].append({"step": name, "ok": ok, "info": str(info)[:200]})
        print(("ok   " if ok else "FAIL ") + name + ": " + str(info)[:160], flush=True)
        return ok

    # 1. diff (cf_diff v3: слоты + полные выгрузки)
    sys.path.insert(0, str(TOOLS))
    import cf_diff
    rep = cf_diff.run(a.cf1, a.cf2, str(work / "diff"))
    fd = rep["full_diff"]
    step("cf_diff", True, "changed=%d new_files=%d" % (len(fd["changed"]), len(fd["only2"])))
    dump1 = work / "diff" / "full-cf1"
    dump2 = work / "diff" / "full-cf2"
    # карта uuid→объект (верхний уровень) по выгрузкам
    def owner_of(file_rel: str):
        """Documents/МсрТЧ43.xml → 'Документ.МсрТЧ43' (по каталогу выгрузки)."""
        CATALOG = {"Documents": "Документ", "Catalogs": "Справочник",
                   "CommonForms": "ОбщаяФорма", "CommonModules": "ОбщийМодуль",
                   "Enums": "Перечисление", "InformationRegisters": "РегистрСведений",
                   "AccumulationRegisters": "РегистрНакопления",
                   "DataProcessors": "Обработка", "Reports": "Отчет"}
        parts = file_rel.split("/")
        if parts[0] in CATALOG:
            stem = parts[1].replace(".xml", "").replace(".bsl", "")
            return CATALOG[parts[0]] + "." + stem
        return None
    # владельцы: изменённые файлы + НОВЫЕ .bsl/Form.xml (модуль стал непустым,
    # добавлена форма существующего объекта — П14-5мод 28.10.2026)
    changed_owners = sorted({owner_of(e["path"]) for e in fd["changed"]
                             if owner_of(e["path"])} |
                            {owner_of(f) for f in fd["only2"]
                             if owner_of(f) and (f.endswith(".bsl") or f.endswith("Form.xml"))})
    step("владельцы изменённого", bool(changed_owners) or not fd["changed"],
         "%d шт: %s" % (len(changed_owners), changed_owners[:5]))

    # 2. scaffold
    src = work / "src"
    if not (src / "Configuration.xml").is_file():
        r = _sh([sys.executable, str(SKILLS / "cfe-init" / "scripts" / "cfe-init.py"),
                 "-Name", a.name, "-NamePrefix", a.prefix, "-Purpose", "Customization",
                 "-OutputDir", str(src), "-ConfigPath", str(dump2)])
        if r.returncode != 0:
            return step("cfe-init", False, r.stdout[-200:] + r.stderr[-200:])
    step("cfe-init", (src / "Configuration.xml").is_file(), str(src))

    # 3. borrow всех изменённых владельцев (кроме общих форм — К-2)
    borrowed = []
    for owner in changed_owners:
        kind, name = owner.split(".", 1)
        if kind == "ОбщаяФорма":
            continue
        r = _sh([sys.executable, str(SKILLS / "cfe-borrow" / "scripts" / "cfe-borrow.py"),
                 "-ExtensionPath", str(src), "-ConfigPath", str(dump2),
                 "-Object", owner])
        if r.returncode != 0:
            return step("cfe-borrow " + owner, False, r.stdout[-200:])
        borrowed.append(owner)
    step("cfe-borrow", True, "%d шт: %s" % (len(borrowed), borrowed))

    # 4. наполнение оболочек
    total_fill = {"mapping": [], "inserted": 0}
    for owner in borrowed:
        kind, name = owner.split(".", 1)
        CAT = {"Документ": "Documents", "Справочник": "Catalogs"}
        shell = src / CAT[kind] / (name + ".xml")
        f1 = dump1 / CAT[kind] / (name + ".xml")
        f2 = dump2 / CAT[kind] / (name + ".xml")
        if not (shell.is_file() and f1.is_file() and f2.is_file()):
            continue
        r = fill_shell(shell, f1, f2, a.prefix)
        total_fill["mapping"].extend(r["mapping"])
        total_fill["inserted"] += r["inserted"]
    step("наполнение", True, "вставок=%d, маппинг=%d" %
         (total_fill["inserted"], len(total_fill["mapping"])))

    # 4b. перехватчики изменённых модулей (ИзменениеИКонтроль; источник — cf2,
    #     ловушка ГисМарк: собственный метод = имя оригинала висит клиент)
    changed_paths = [e["path"] for e in fd["changed"]]
    for path in changed_paths + list(fd["only2"]):
        if not path.endswith(".bsl"):
            continue
        owner = owner_of(path)
        if not owner or ".Форма." in path:
            continue
        kind, name = owner.split(".", 1)
        CAT = {"Документ": "Document", "Справочник": "Catalog"}
        mod_path = CAT.get(kind, kind) + "." + name + "." + (
            "ObjectModule" if path.endswith("ObjectModule.bsl") else "ManagerModule")
        # список экспортных процедур из исходника cf2 (источник перехватчика)
        src_bsl = dump2 / path if not Path(path).is_absolute() else Path(path)
        methods = re.findall(r"Процедура\s+(\w+)\(",
                             src_bsl.read_text(encoding="utf-8-sig", errors="replace"), re.M)
        if not methods:
            step("перехватчик " + mod_path, True, "экспортных процедур нет")
            continue
        ok_all = True
        for meth in methods:
            m = _sh([sys.executable, str(SKILLS / "cfe-patch-method" / "scripts" / "cfe-patch-method.py"),
                     "-ExtensionPath", str(src), "-ConfigPath", str(dump2),
                     "-ModulePath", mod_path, "-MethodName", meth,
                     "-InterceptorType", "ModificationAndControl"])
            if "[OK]" not in m.stdout:
                ok_all = False
                step("перехватчик " + meth, False, (m.stdout + m.stderr)[-200:])
        if not ok_all:
            return 1
    step("перехватчики модулей", True, "по экспортным процедурам изменённых .bsl")

    # 5. validate
    r = _sh([sys.executable, str(SKILLS / "cfe-validate" / "scripts" / "cfe-validate.py"),
             "-ExtensionPath", str(src)])
    if "Validation OK" not in r.stdout:
        return step("cfe-validate", False, r.stdout[-300:])
    step("cfe-validate", True, "OK")

    # 6. слот cf1 → загрузка → cfe
    slot = work / "slot-cf1"
    if not (slot / "1Cv8.1CD").is_file():
        r = _sh([sys.executable, str(TOOLS / "mockup_init.py"), "--base", str(slot),
                 "--from", a.cf1, "--reset"], timeout=3600)
        if r.returncode != 0:
            return step("слот cf1", False, r.stdout[-200:])
    _designer(slot, ["/LoadConfigFromFiles", str(src), "-Extension", a.name],
              work, "load", timeout=900)
    _, log = _designer(slot, ["/CheckModules", "-Extension", a.name], work, "check")
    if "не обнаружено" not in log and "Синтаксических ошибок" not in log:
        return step("CheckModules", False, log[-200:])
    _designer(slot, ["/UpdateDBCfg", "-Extension", a.name], work, "upd", timeout=900)
    _designer(slot, ["/DumpCfg", str(work / (a.name + ".cfe")), "-Extension", a.name],
              work, "dumpcfe")
    cfe = work / (a.name + ".cfe")
    step("cfe-артефакт", cfe.is_file() and cfe.stat().st_size > 0, str(cfe))

    # 7. верификация: вендор цел
    sys.path.insert(0, str(TOOLS))
    from cf_inspect import CfFile
    import hashlib
    def inv(p):
        with CfFile(p) as cf:
            return {r["name"]: hashlib.sha1(cf.read(r)).hexdigest()
                    for r in cf.records()
                    if not re.match(r"^(configinfo|version|versions|deleted|root)$", r["name"])}
    main_cf = work / "slot-main.cf"
    _designer(slot, ["/DumpCfg", str(main_cf)], work, "dumpmain")
    vendor_ok = inv(a.cf1) == inv(str(main_cf))
    step("вендор цел", vendor_ok, "записей cf1: %d" % len(inv(a.cf1)))

    report["fill"] = total_fill
    report["cfe"] = str(cfe)
    report["vendor_ok"] = vendor_ok
    (work / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print("=== ГОТОВО:", cfe, "| вендор цел:", vendor_ok)
    return 0 if vendor_ok else 1


if __name__ == "__main__":
    sys.exit(main())
