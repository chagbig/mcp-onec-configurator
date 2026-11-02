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
7. Верификации: вендор цел (снимок слота до/после загрузки расширения — загрузка
   расширения не изменила основную конфигурацию),
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


def _child_index(owner_xml: str, which: str = "top") -> dict:
    """Индекс объектов владельца по уровням (ревью v1.0.0 К-1/С-1):
    which="top" — прямые дети владельческого ChildObjects (ТЧ + реквизиты верхнего
    уровня); which="tch" — объединение прямых детей ChildObjects КАЖДОЙ ТЧ
    (реквизиты табличных частей). Парс через обёртку с ns-декларациями: вырезанные
    фрагменты несут unbound префиксы (v8:, xr:)."""
    import xml.etree.ElementTree as ET
    out = {}
    NS = ('xmlns:v8="http://v8.1c.ru/8.1/data/core" '
          'xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
          'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')
    body = re.sub(r"^<[?]xml[^>]*[?]>", "", owner_xml.strip())
    try:
        root = ET.fromstring("<Wrapper %s>" % NS + body + "</Wrapper>")
    except ET.ParseError:
        return out
    cos = [el for el in root.iter() if el.tag.endswith("ChildObjects")]
    if not cos:
        return out
    if which == "tch":
        for ts in root.iter():
            if not ts.tag.endswith("TabularSection"):
                continue
            for co in list(ts):
                if co.tag.endswith("ChildObjects"):
                    for el in list(co):
                        kind = el.tag.split("}")[-1]
                        if kind not in ("Attribute",):
                            continue
                        uuid = el.get("uuid")
                        nm = el.find(".//{*}Name")
                        if uuid and nm is not None and nm.text:
                            out[(kind, nm.text)] = uuid
        return out
    # top: владельческий ChildObjects — первый, чьи дети содержат TabularSection;
    # иначе первый (реквизиты верхнего уровня)
    co = cos[0]
    for c in cos:
        if any(ch.tag.endswith("TabularSection") for ch in list(c)):
            co = c
            break
    for el in list(co):
        kind = el.tag.split("}")[-1]
        if kind not in ("Attribute", "TabularSection", "Form", "Command",
                        "Dimension", "Resource"):
            continue
        uuid = el.get("uuid")
        nm = el.find(".//{*}Name")
        if uuid and nm is not None and nm.text:
            out[(kind, nm.text)] = uuid
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
    mapping, added_tch, added_attr = [], 0, 0
    inserts = []

    # новые в cf2 собственные части ВЕРХНЕГО уровня владельца (реквизиты ТЧ —
    # в Adopted-ветви ниже; здесь отбираем только прямых детей, не вложенных)
    direct2 = _child_index(c2)
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
        # КРИТИЧНО (ревью v1.0.0 К-1): sub1 — из ТЧ CF1 (было frag2 — ветвь была мертва)
        frag1 = _fragment(c1, kind, uuid1) or ""
        sub1 = _child_index(frag1, which="tch")
        sub2 = _child_index(frag2, which="tch")
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
        # cf1-ТЧ может быть <ChildObjects/> (пустая самозакрытая) — покрываем обе формы
        parts_block = "\n".join(parts)
        if re.search(r"<ChildObjects\s*/>", adopted):
            adopted = re.sub(r"<ChildObjects\s*/>",
                             "<ChildObjects>" + parts_block + "</ChildObjects>", adopted, count=1)
        else:
            adopted = re.sub(r"<ChildObjects>.*?</ChildObjects>",
                             "<ChildObjects>" + parts_block + "</ChildObjects>", adopted, count=1, flags=re.S)
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


# каталоги-владельцы форм: <каталог>/<Объект>/Forms/<Форма>/Ext/Form.xml|Form/Module.bsl
FORMS_RE = re.compile(
    r"^(Documents|Catalogs|DataProcessors|Reports|Enums|InformationRegisters|"
    r"AccumulationRegisters|ChartsOfAccounts|ChartsOfCharacteristicTypes|"
    r"ExchangePlans|BusinessProcesses|Tasks)/([^/]+)/Forms/([^/]+)/"
    r"Ext(?:/Form)?/(Form\.xml|Module\.bsl)$")
DIR2RU = {"Documents": "Документ", "Catalogs": "Справочник", "DataProcessors": "Обработка",
          "Reports": "Отчет", "Enums": "Перечисление",
          "InformationRegisters": "РегистрСведений",
          "AccumulationRegisters": "РегистрНакопления",
          "ChartsOfAccounts": "ПланСчетов", "ChartsOfCharacteristicTypes": "ПланВидовХарактеристик",
          "ExchangePlans": "ПланОбмена", "BusinessProcesses": "БизнесПроцесс",
          "Tasks": "Задача"}
RU2DIR = {v: k for k, v in DIR2RU.items()}
RU2EN = {"Документ": "Document", "Справочник": "Catalog", "Обработка": "DataProcessor",
         "Отчет": "Report", "Перечисление": "Enum", "РегистрСведений": "InformationRegister",
         "РегистрНакопления": "AccumulationRegister", "ПланСчетов": "ChartOfAccounts",
         "ПланВидовХарактеристик": "ChartOfCharacteristicTypes",
         "ПланОбмена": "ExchangePlan", "БизнесПроцесс": "BusinessProcess", "Задача": "Task"}


def collect_form_targets(changed: list, only2: list, only1: list) -> tuple[dict, list]:
    """Детект форм (этап 15 + ревью C1): FORMS_RE по изменённым, новым И удалённым
    путям. Возвращает (form_rels {(dir, owner, form): True}, deleted_paths) —
    форму с удалённым в cf2 файлом (например только Module.bsl) надо пропустить
    через fill_form, чтобы она в src привелась к cf2 (модуль снялся); полную
    форму, которой нет в cf2, вызывающий уводит в отчёт."""
    rels: dict = {}
    deleted: list = []
    for path in list(changed) + list(only2) + list(only1):
        m = FORMS_RE.match(path)
        if not m:
            continue
        rels[(m.group(1), m.group(2), m.group(3))] = True
        if path in only1:
            deleted.append(path)
    return rels, deleted


def _norm_xml(text: str) -> bytes:
    """Нормализованное дерево XML (канонизация платформой не влияет на сравнение)."""
    import xml.etree.ElementTree as ET
    body = re.sub(r"^<[?]xml[^>]*[?]>", "", text.strip())
    return ET.tostring(ET.fromstring(body.encode("utf-8")))


# платформа в замещающей форме расширения: свойства владельца-документа выкидывает
# (держит их на владельце), у Event/Action проставляет callType (семантика перехвата),
# ссылки на вендорские типы разворачивает в uuid с переименованием тега
# (Type/TypeSet -> TypeId; Item функциональных опций — тоже uuid) — П15-5
_FORM_CANON_PROPS = ("AutoTime", "UsePostingMode", "RepostOnWrite")
_FORM_CANON_REF_TAGS = ("Type", "TypeSet", "TypeId", "Item")


def _norm_form_xml(text: str) -> bytes:
    """Нормализация Form.xml замещающей формы с учётом канонизации платформы.
    Ссылки на типы сравниваются структурно: корректность ссылок проверяет сама
    загрузка (платформа разрешила имена), постусловие ловит потерю содержимого."""
    import xml.etree.ElementTree as ET
    body = re.sub(r"^<[?]xml[^>]*[?]>", "", text.strip())
    root = ET.fromstring(body.encode("utf-8"))
    for el in list(root):
        if el.tag.split("}")[-1] in _FORM_CANON_PROPS:
            root.remove(el)
    for el in root.iter():
        for attr in [a for a in el.attrib if a == "callType" or a.endswith("}callType")]:
            del el.attrib[attr]
        tag = el.tag.split("}")[-1]
        if tag in ("Type", "TypeSet", "TypeId"):
            head, _, _ = el.tag.rpartition("}")
            el.tag = head + "}REF" if head else "REF"
        if tag in _FORM_CANON_REF_TAGS:
            el.text = None
    return ET.tostring(root)


def fill_form(src: Path, dump1: Path, dump2: Path, dir_name: str, owner: str,
              form: str) -> dict:
    """Перенос формы как замещающей (этап 15, Г1): ОБОЛОЧКУ (wrapper Adopted +
    ExtendedConfigurationObject) создаёт cfe-borrow -Object <Тип>.<Объект>.Form.<Форма>;
    здесь в оболочку переносятся ПОЛНЫЙ контент Form.xml из cf2 (без урезаний
    cfe-borrow) и модуль формы (Ext/Form/Module.bsl — канон выгрузки 8.3.27).
    Идемпотентно: совпадающий контент не переписывается."""
    base = Path(dir_name) / owner / "Forms" / form
    src_xml = src / base / "Ext" / "Form.xml"
    cf2_xml = dump2 / base / "Ext" / "Form.xml"
    if not cf2_xml.is_file():
        return {"ok": False, "err": "в cf2 нет Form.xml: %s" % base.as_posix()}
    if not src_xml.is_file():
        return {"ok": False, "err": "borrow не создал оболочку формы: %s" % src_xml}
    # постусловие оболочки: Adopted + ссылка на uuid формы cf1
    wrapper = src / base.parent / (form + ".xml")
    w = wrapper.read_text(encoding="utf-8-sig") if wrapper.is_file() else ""
    m1 = re.search(r"<Form[^>]*uuid=\"([0-9a-f-]{36})\"", w)
    if "Adopted" not in w or "<ExtendedConfigurationObject>" not in w or not m1:
        return {"ok": False, "err": "оболочка не Adopted/без ExtendedConfigurationObject: %s"
                                     % wrapper.name}
    cf1_uuid = (dump1 / base.parent / (form + ".xml")).read_text(encoding="utf-8-sig") \
        if (dump1 / base.parent / (form + ".xml")).is_file() else ""
    mcf1 = re.search(r"<Form[^>]*uuid=\"([0-9a-f-]{36})\"", cf1_uuid)
    if not mcf1 or mcf1.group(1) != w.split("<ExtendedConfigurationObject>")[1].split("<")[0]:
        return {"ok": False, "err": "ExtendedConfigurationObject != uuid формы cf1"}
    out = {"ok": True, "ext_uuid": m1.group(1), "cf1_uuid": mcf1.group(1), "changed": False}
    # контент: полный cf2-XML, идемпотентность по нормализованному дереву;
    # битый XML (П15-4) — отказ ДО сборки, без traceback
    try:
        new_content = cf2_xml.read_text(encoding="utf-8-sig")
        src_norm = _norm_xml(src_xml.read_text(encoding="utf-8-sig"))
        cf2_norm = _norm_xml(new_content)
    except Exception as e:  # ET.ParseError и родственные
        return {"ok": False, "err": "битый Form.xml (%s): %s" % (base.as_posix(), e)}
    if src_norm != cf2_norm:
        src_xml.write_text(new_content, encoding="utf-8-sig")
        out["changed"] = True
    # модуль формы: cf2 — источник истины (в cf1 может не быть; тогда заглушка borrow удаляется)
    cf2_mod = dump2 / base / "Ext" / "Form" / "Module.bsl"
    src_mod = src / base / "Ext" / "Form" / "Module.bsl"
    if cf2_mod.is_file():
        mod = cf2_mod.read_text(encoding="utf-8-sig")
        if not src_mod.is_file() or src_mod.read_text(encoding="utf-8-sig") != mod:
            src_mod.write_text(mod, encoding="utf-8-sig")
            out["changed"] = True
    elif src_mod.is_file():
        src_mod.unlink()
        out["changed"] = True
    return out


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

    # ревью C2 (fail fast): слот на work собирается по КОНКРЕТНОМУ cf1 — повторный
    # прогон другой пары на том же work со старым слотом валидировал бы расширение
    # против чужого вендора; проверка ДО diff/borrow, чтобы не делать лишнюю работу
    origin = {"cf1": str(Path(a.cf1).resolve()).lower(),
              "size": Path(a.cf1).stat().st_size}
    slot_marker = work / "slot-cf1.origin.json"
    if slot_marker.is_file():
        prev = json.loads(slot_marker.read_text(encoding="utf-8"))
        if prev != origin:
            print("FAIL слот cf1: work собран по другому cf1 (%s, %s б) — "
                  "укажите чистый --work" % (prev.get("cf1"), prev.get("size")),
                  flush=True)
            report["steps"].append({"step": "слот cf1", "ok": False,
                                    "info": "другой cf1 на том же work"})
            (work / "report.json").write_text(json.dumps(report, ensure_ascii=False,
                                                         indent=1), encoding="utf-8")
            return 1

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
    # владельцы: изменённые файлы + НОВЫЕ .bsl/Form.xml, но ТОЛЬКО если владелец
    # существует в cf1 (новые объекты cf2 НЕПЕРЕНОСИМЫ — §1, в отчёт; ревью M1)
    _cf1_files = {p.relative_to(dump1).as_posix() for p in dump1.rglob("*") if p.is_file()}

    def _in_cf1(rel):
        return rel in _cf1_files or rel.replace("/Ext/", "/") in _cf1_files

    changed_owners = sorted(
        {owner_of(e["path"]) for e in fd["changed"] if owner_of(e["path"])} |
        {owner_of(f) for f in fd["only2"]
         if owner_of(f) and (f.endswith(".bsl") or f.endswith("Form.xml")) and _in_cf1(f)})
    report["new_objects_cf2"] = sorted({owner_of(f) for f in fd["only2"] if owner_of(f)
                                        and owner_of(f) not in changed_owners})
    step("владельцы изменённого", bool(changed_owners) or not fd["changed"],
         "%d шт: %s" % (len(changed_owners), changed_owners[:5]))
    if not changed_owners and fd["changed"]:
        step("владельцы изменённого", False,
             "отличия не отображаются на объекты верхнего уровня — пустое расширение")
        report["transfer"] = fd
        (work / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
        return 1

    # 2. scaffold
    src = work / "src"
    if not (src / "Configuration.xml").is_file():
        r = _sh([sys.executable, str(SKILLS / "cfe-init" / "scripts" / "cfe-init.py"),
                 "-Name", a.name, "-NamePrefix", a.prefix, "-Purpose", "Customization",
                 "-OutputDir", str(src), "-ConfigPath", str(dump2)])
        if r.returncode != 0:
            step("cfe-init", False, r.stdout[-200:] + r.stderr[-200:])
            return 1
    step("cfe-init", (src / "Configuration.xml").is_file(), str(src))

    # 3. borrow всех изменённых владельцев (кроме общих форм — К-2)
    # ВАЖНО (ревью M2c): cfe-borrow перезаписывает оболочки <ChildObjects/> —
    # ручные правки в src между прогонами будут потеряны; наполнение fill_shells
    # восстанавливается автоматически, ручные — нет
    if (src / "Configuration.xml").is_file():
        report["warning"] = "src уже существует — borrow перезапишет оболочки; ручные правки src будут потеряны"
        print("WARN: src уже существует — оболочки будут перезаписаны")
    borrowed = []
    for owner in changed_owners:
        kind, name = owner.split(".", 1)
        if kind == "ОбщаяФорма":
            continue
        r = _sh([sys.executable, str(SKILLS / "cfe-borrow" / "scripts" / "cfe-borrow.py"),
                 "-ExtensionPath", str(src), "-ConfigPath", str(dump2),
                 "-Object", owner])
        if r.returncode != 0:
            step("cfe-borrow " + owner, False, r.stdout[-200:])
            return 1
        borrowed.append(owner)
    step("cfe-borrow", True, "%d шт: %s" % (len(borrowed), borrowed))

    # 4. наполнение оболочек
    total_fill = {"mapping": [], "inserted": 0}
    for owner in borrowed:
        kind, name = owner.split(".", 1)
        # ревью M4: карта на ВСЕ виды (было только Документ/Справочник —
        # KeyError на Обработка и прочих из скоупа этапа 15)
        d = RU2DIR[kind]
        shell = src / d / (name + ".xml")
        f1 = dump1 / d / (name + ".xml")
        f2 = dump2 / d / (name + ".xml")
        if not (shell.is_file() and f1.is_file() and f2.is_file()):
            continue
        r = fill_shell(shell, f1, f2, a.prefix)
        total_fill["mapping"].extend(r["mapping"])
        total_fill["inserted"] += r["inserted"]
    step("наполнение", True, "вставок=%d, маппинг=%d" %
         (total_fill["inserted"], len(total_fill["mapping"])))

    # 4a. формы: замещающие (этап 15) — изменённые формы объектов, существующих в cf1.
    # Общие формы (CommonForms) и новые формы cf2 без оригинала — вне скоупа, в отчёт.
    changed_paths = [e["path"] for e in fd["changed"]]
    form_rels, deleted_form_paths = collect_form_targets(
        changed_paths, list(fd["only2"]), list(fd["only1"]))
    report["forms_skipped"] = sorted(
        p for p in changed_paths + list(fd["only2"]) + list(fd["only1"])
        if "/Forms/" in p and not FORMS_RE.match(p))
    forms_report = []
    for dir_name, owner, form in sorted(form_rels):
        # П15-4: новая форма cf2 без оригинала в cf1 — вне скоупа (привязки не
        # проверить), в отчёт, конвейер не роняем
        if not (dump1 / dir_name / owner / "Forms" / (form + ".xml")).is_file():
            report.setdefault("new_forms_cf2", []).append(
                "%s.%s.%s" % (DIR2RU[dir_name], owner, form))
            continue
        # ревью C1: файл формы удалён в cf2 целиком — заимствовать не из чего,
        # в отчёт (частичное удаление — например только модуль — обрабатывается
        # fill_form: форма приводится к cf2, модуль снимается)
        if not (dump2 / dir_name / owner / "Forms" / (form + ".xml")).is_file():
            report.setdefault("deleted_forms_cf2", []).append(
                "%s.%s.%s" % (DIR2RU[dir_name], owner, form))
            continue
        target = "%s.%s.Form.%s" % (DIR2RU[dir_name], owner, form)
        r = _sh([sys.executable, str(SKILLS / "cfe-borrow" / "scripts" / "cfe-borrow.py"),
                 "-ExtensionPath", str(src), "-ConfigPath", str(dump2), "-Object", target])
        if r.returncode != 0:
            step("cfe-borrow " + target, False, (r.stdout + r.stderr)[-200:])
            return 1
        fr = fill_form(src, dump1, dump2, dir_name, owner, form)
        if not fr["ok"]:
            step("fill_form " + target, False, fr["err"])
            return 1
        forms_report.append({"dir": dir_name, "owner": owner, "form": form,
                             "ext_uuid": fr["ext_uuid"], "cf1_uuid": fr["cf1_uuid"],
                             "changed": fr["changed"]})
        step("форма " + target, True, "uuid %s → %s" % (fr["cf1_uuid"][:8], fr["ext_uuid"][:8]))
    report["forms"] = forms_report

    # 4b. перехватчики изменённых модулей (ИзменениеИКонтроль; источник — cf2,
    #     ловушка ГисМарк: собственный метод = имя оригинала висит клиент)
    for path in changed_paths + list(fd["only2"]):
        if not path.endswith(".bsl"):
            continue
        if "/Forms/" in path:
            continue  # модуль формы переносится ЦЕЛИКОМ (этап 15) — перехват не нужен
        owner = owner_of(path)
        if not owner or ".Форма." in path:
            continue
        kind, name = owner.split(".", 1)
        # ревью M4: английское имя вида для cfe-patch-method — на всех видах
        # (было только Документ/Справочник, прочие уходили в -ModulePath кириллицей)
        mod_path = RU2EN.get(kind, kind) + "." + name + "." + (
            "ObjectModule" if path.endswith("ObjectModule.bsl") else "ManagerModule")
        # список экспортных процедур из исходника cf2 (источник перехватчика)
        src_bsl = dump2 / path if not Path(path).is_absolute() else Path(path)
        # список процедур/функций из исходника cf2 (источник перехватчика);
        # ^-якорь: комментарии "// Процедура" не матчатся; директивы & пропускаются
        methods = re.findall(
            r"^(?:&[^\r\n]*[\r\n]+)*(?:Процедура|Функция)\s+(\w+)\s*\(",
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
            out = m.stdout + m.stderr
            # скил идемпотентен: [ОК] создан, [АКТУАЛЕН]/[АКТУАЛИЗИРОВАН] — уже есть
            if not any(t in out for t in ("[OK]", "[АКТУАЛЕН]", "[АКТУАЛИЗИРОВАН]",
                                          "[ПЕРЕНЕСЕНО В ОСНОВНУЮ]")):
                ok_all = False
                step("перехватчик " + meth, False, out[-200:])
        if not ok_all:
            return 1
    step("перехватчики модулей", True, "по экспортным процедурам изменённых .bsl")

    # 5. validate
    r = _sh([sys.executable, str(SKILLS / "cfe-validate" / "scripts" / "cfe-validate.py"),
             "-ExtensionPath", str(src)])
    if "Validation OK" not in r.stdout:
        step("cfe-validate", False, r.stdout[-300:])
        return 1
    step("cfe-validate", True, "OK")

    # 6. слот cf1 → загрузка → cfe
    slot = work / "slot-cf1"
    if not (slot / "1Cv8.1CD").is_file() or not slot_marker.is_file():
        # нет слота или нет маркера происхождения (проверка cf1 выше) — пересборка
        # гарантирует, что слот соответствует объявленному cf1
        r = _sh([sys.executable, str(TOOLS / "mockup_init.py"), "--base", str(slot),
                 "--from", a.cf1, "--reset"], timeout=3600)
        if r.returncode != 0:
            step("слот cf1", False, r.stdout[-200:])
            return 1
        slot_marker.write_text(json.dumps(origin), encoding="utf-8")
        step("слот cf1", True, "готов (пересоздан)")
    # базовый снимок ДО загрузки расширения: вендор цел = загрузка расширения не
    # изменила основную конфигурацию (сравнение с cf1 напрямую ложно падает —
    # круг «cf -> слот» пересохраняет служебные записи, П15-5: 32 из 45355)
    baseline = work / "slot-baseline.cf"
    _designer(slot, ["/DumpCfg", str(baseline)], work, "dumpbaseline", timeout=1800)
    _designer(slot, ["/LoadConfigFromFiles", str(src), "-Extension", a.name],
              work, "load", timeout=900)
    _, log = _designer(slot, ["/CheckModules", "-Extension", a.name], work, "check")
    if "не обнаружено" not in log and "Синтаксических ошибок" not in log:
        step("CheckModules", False, log[-200:])
        return 1
    _designer(slot, ["/UpdateDBCfg", "-Extension", a.name], work, "upd", timeout=900)
    # 6b. постусловие форм (этап 15): платформа приняла замещающие формы —
    # выгрузка расширения содержит контент cf2 (нормализованное сравнение)
    if form_rels:
        ext_dump = work / "dump-ext"
        shutil.rmtree(ext_dump, ignore_errors=True)  # ревью C3: stale-файлы прошлых прогонов
        _designer(slot, ["/DumpConfigToFiles", str(ext_dump), "-Extension", a.name],
                  work, "dumpext", timeout=1800)
        for fr in forms_report:
            base = Path(fr["dir"]) / fr["owner"] / "Forms" / fr["form"]
            ext_xml = ext_dump / base / "Ext" / "Form.xml"
            cf2_xml = dump2 / base / "Ext" / "Form.xml"
            ok_xml = ext_xml.is_file() and \
                _norm_form_xml(ext_xml.read_text(encoding="utf-8-sig")) == \
                _norm_form_xml(cf2_xml.read_text(encoding="utf-8-sig"))
            ext_mod = ext_dump / base / "Ext" / "Form" / "Module.bsl"
            cf2_mod = dump2 / base / "Ext" / "Form" / "Module.bsl"
            ok_mod = cf2_mod.is_file() == ext_mod.is_file() and (
                not cf2_mod.is_file() or
                cf2_mod.read_text(encoding="utf-8-sig") == ext_mod.read_text(encoding="utf-8-sig"))
            if not (ok_xml and ok_mod):
                step("постусловие формы " + fr["form"], False,
                     "xml=%s mod=%s" % (ok_xml, ok_mod))
                return 1
        step("постусловие форм", True, "%d шт: контент cf2 в выгрузке расширения"
             % len(forms_report))
    cfe = work / (a.name + ".cfe")
    cfe.unlink(missing_ok=True)  # ревью C3: прошлый cfe не должен сойти за результат
    _designer(slot, ["/DumpCfg", str(cfe), "-Extension", a.name],
              work, "dumpcfe")
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
    vendor_ok = inv(str(baseline)) == inv(str(main_cf))
    step("вендор цел", vendor_ok, "записей: %d (слот до/после расширения)" % len(inv(str(baseline))))

    report["fill"] = total_fill
    report["manual"] = rep["verdicts"]["manual"]
    report["cfe"] = str(cfe)
    report["vendor_ok"] = vendor_ok
    (work / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print("=== ГОТОВО:", cfe, "| вендор цел:", vendor_ok)
    return 0 if vendor_ok else 1


if __name__ == "__main__":
    sys.exit(main())
