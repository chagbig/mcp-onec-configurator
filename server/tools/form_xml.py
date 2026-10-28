# -*- coding: utf-8 -*-
"""Чтение и запись формы через cf + temp-базу (этап 8, цепочки §3.1/§3.2 ред. 3).

Чтение (form_get_xml):
  предусловие isModified=false → cf (свежий или переданный) → temp_read
  (mockup_init --from, кэш по имени cf) → /DumpConfigToFiles формы → XML.

Запись (form_set_xml):
  предусловие isModified=false + снимок состава владельца == ожидаемому (К2) →
  cf → temp_write (пересобирается на каждую запись, М3) → dump формы → правка
  вызывающим через callback → /LoadConfigFromFiles (пути файлов, UTF-8) →
  постусловие re-dump == выгрузке после первой загрузки (М5) → /DumpCfg → cf2 →
  конфигуратор живой базы закрывается (isModified=false проверен) → /LoadCfg cf2 →
  /UpdateDBCfg → конфигуратор открывается → верификация: cf живой базы текст формы
  == cf2 (нормализатор шапки+EOL), form_save содержит признак правки.

П3 28.10.2026: запись через живой редактор (form_import) НЕ работает — буфер
редактора изолирован от конфигурации; поэтому /LoadCfg (docs/stage-08-forms.md §4.3).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).parent
sys.path.insert(0, str(TOOLS.parent))
sys.path.insert(0, str(TOOLS))

from mcp_snegopat.bridge import Bridge, BridgeError  # noqa: E402
from mcp_snegopat.config import load_config  # noqa: E402
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("mtl", TOOLS / "mcp_to_line.py")
mtl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mtl)

PLATFORM = mtl.PLATFORM
LOGS = Path(r"Y:\Snegopat-mcp\logs")
WORK = Path(r"Y:\Snegopat-mcp\verify\form_xml")
CACHE = Path(r"Y:\Snegopat-mcp\cache\formxml")


class FormXmlError(Exception):
    pass


BACKUPS = Path(r"Y:\Snegopat-mcp\backups")
WORK_GC_KEEP = 2            # сколько старых work-каталогов переживают уборку
WORK_GC_MIN_AGE_S = 3600.0  # каталоги моложе часа уборка не трогает (чужие прогоны)


def _validate_form_xml(xml_text: str) -> None:
    """Лёгкая валидация ДО тяжёлой цепочки: well-formed + корневой элемент Form
    (ns logform). Битый XML иначе обнаружился бы только в постусловии М5 —
    после минут пересборки temp_write."""
    from lxml import etree
    try:
        root = etree.fromstring(xml_text.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        raise FormXmlError(f"XML не разбирается (правка отклонена до цепочки): {e}")
    want = "{http://v8.1c.ru/8.3/xcf/logform}Form"
    if root.tag != want:
        raise FormXmlError(
            f"корневой элемент {root.tag}, ожидается {want} — правка отклонена до цепочки")


def _gc_work(root: Path, keep: int = WORK_GC_KEEP,
             min_age_s: float = WORK_GC_MIN_AGE_S,
             exclude: Path | None = None) -> list[str]:
    """Retention тяжёлых work-каталогов (temp-базы по 0,004–6 ГБ): удаляются
    каталоги старше min_age_s, кроме keep свежайших; exclude (текущий прогон)
    не трогается. Ошибки (антивирусные блокировки) молча пропускаются."""
    if not root.is_dir():
        return []
    now = time.time()

    def age(p: Path) -> float:
        try:
            return now - p.stat().st_mtime
        except OSError:
            return 0.0

    dirs = [d for d in root.iterdir() if d.is_dir() and d != exclude]
    cands = sorted((d for d in dirs if age(d) > min_age_s), key=age)
    doomed = cands[:len(cands) - keep] if keep else list(cands)
    removed = []
    for d in doomed:
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            removed.append(d.name)
    return removed


def _gc_arch_cfs(backups: Path, min_age_s: float = WORK_GC_MIN_AGE_S,
                 extra_keep: Path | None = None) -> list[str]:
    """Архивы mcp-formxml-*.cf копятся в backups по 2 ГБ за каждое чтение без
    cf_path: удаляются файлы старше min_age_s, кроме упомянутых в реестре cf и
    extra_keep (cf текущего прогона)."""
    refs = set()
    try:
        reg = json.loads(_cf_registry_path().read_text(encoding="utf-8"))
        refs = {Path(v["cf"]).resolve() for v in reg.values()}
    except Exception:
        pass
    if extra_keep is not None:
        refs.add(extra_keep.resolve())
    now = time.time()
    removed = []
    if not backups.is_dir():
        return removed
    for f in backups.glob("mcp-formxml-*.cf"):
        try:
            if f.resolve() in refs or now - f.stat().st_mtime <= min_age_s:
                continue
            f.unlink()
            removed.append(f.name)
        except OSError:
            continue
    return removed


def _gc_housekeeping(current_work: Path, current_cf: Path | None) -> dict:
    """Уборка после операции; никогда не роняет её."""
    try:
        return {"work_dirs": _gc_work(WORK, exclude=current_work),
                "arch_cfs": _gc_arch_cfs(BACKUPS, extra_keep=current_cf)}
    except Exception as e:
        return {"error": str(e)[:120]}


def _designer(args, log, res, timeout=1800):
    ok, info = mtl.run_designer(args, log, res, timeout)
    if not ok:
        raise FormXmlError(info)
    return info


def _mockup_init(base: Path, cf: Path, timeout=3600):
    # --reset удаляет базу; свежесозданный/залитый 1,9 ГБ каталог Windows/антивирус
    # держит секунды — ретраим удаление (28.10.2026: «занят и не удаляется»)
    last = ""
    for attempt in range(6):
        r = subprocess.run(
            [sys.executable, str(TOOLS / "mockup_init.py"), "--base", str(base),
             "--from", str(cf), "--reset", "--dir", str(LOGS)],
            capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return
        last = f"{r.stdout[-200:]} {r.stderr[-200:]}"
        if "занят" not in last:
            break
        time.sleep(15)
    raise FormXmlError(f"mockup_init: {last}")


def session_for_base(bridge: Bridge, base: Path) -> int:
    want = mtl.norm_file_key(base)
    for s in bridge.sessions():
        if not s.alive:
            continue
        conn = str(s.data.get("conn") or "")
        if "=" not in conn:
            continue
        value = conn.split("=", 1)[1].rstrip(";\" ")
        if mtl.norm_file_key(Path(value)) == want:
            return s.pid
    raise FormXmlError(f"нет живой сессии конфигуратора на {base}")


def _modified(bridge: Bridge, pid: int) -> bool:
    si = bridge.call(pid, "session_info", {}, timeout=20)
    for c in si.get("containers", []):
        if "Основная" in str(c.get("identifier")) and c.get("isModified"):
            return True
    return False


def snapshot_owner(bridge: Bridge, pid: int, owner_path: str) -> dict:
    """Снимок состава владельца формы (К2): имя+uuid, реквизиты, табличные части."""
    snap = {"path": owner_path}
    obj = bridge.call(pid, "md_object", {"path": owner_path}, timeout=60)
    snap["uuid"] = obj["id"]
    for kind in ("Реквизиты", "ТабличныеЧасти"):
        ch = bridge.call(pid, "md_children",
                         {"path": owner_path, "kind": kind}, timeout=60)
        snap[kind] = [i["name"] for i in ch.get("items", [])]
    return snap


def _arch_cf(bridge: Bridge, pid: int, name: str) -> Path:
    r = bridge.call(pid, "cfg_archive_cf", {"file": name}, timeout=1800)
    if not r.get("exists"):
        raise FormXmlError("архив cf не создан")
    return Path(r["file"])


def _form_uuid(bridge: Bridge, pid: int, obj_path: str, form_name: str) -> str:
    """Uuid записи формы в cf: у общей формы — uuid самого объекта; у подчинённой
    формы — uuid из Формы владельца (имена записей cf = uuid форм, §4.1)."""
    if obj_path.startswith("ОбщаяФорма."):
        return bridge.call(pid, "md_object", {"path": obj_path}, timeout=60)["id"]
    ch = bridge.call(pid, "md_children", {"path": obj_path, "kind": "Формы"}, timeout=60)
    for i in ch.get("items", []):
        if i["name"] == form_name:
            return i["id"]
    raise FormXmlError(f"форма {form_name} у {obj_path} не найдена")


def _cf_key(cf: Path) -> str:
    """Идентичность cf-снимка: имя + размер + mtime_ns. Файлы-архивы создаются
    один раз и не меняются — пара (размер, mtime) однозначно опознаёт снимок,
    полный sha1 2 ГБ не читаем."""
    st = cf.stat()
    return "%s-%d-%d" % (cf.stem, st.st_size, st.st_mtime_ns)


def _read_tread_marker(work: Path) -> dict | None:
    try:
        return json.loads((work / "tread.cf.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_tread_marker(work: Path, cf: Path) -> None:
    st = cf.stat()
    (work / "tread.cf.json").write_text(
        json.dumps({"cf": str(cf), "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                   ensure_ascii=False), encoding="utf-8")


def _tread_reusable(work: Path, cf: Path) -> bool:
    m = _read_tread_marker(work)
    if not m:
        return False
    st = cf.stat()
    return m.get("size") == st.st_size and m.get("mtime_ns") == st.st_mtime_ns


def _cf_registry_path() -> Path:
    return CACHE / "cf_registry.json"


def _cf_registry_get(base: Path) -> dict | None:
    try:
        return json.loads(_cf_registry_path().read_text(encoding="utf-8")).get(
            mtl.norm_file_key(base))
    except Exception:
        return None


def _cf_registry_put(base: Path, cf: Path) -> None:
    p = _cf_registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        reg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        reg = {}
    reg[mtl.norm_file_key(base)] = {"cf": str(cf), "ts": time.time()}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _get_cf(bridge: Bridge, pid: int, base: Path, cf_path: str | None,
            cf_max_age_s: float) -> tuple[Path, str]:
    """cf для чтения: переданный, либо кэш по времени (моложе cf_max_age_s),
    либо свежий архив (факт пишется в реестр для последующих чтений)."""
    if cf_path:
        return Path(cf_path), "given"
    if cf_max_age_s > 0:
        ent = _cf_registry_get(base)
        if ent:
            p = Path(ent["cf"])
            if p.is_file():
                age = time.time() - float(ent["ts"])
                if age <= cf_max_age_s:
                    return p, "cf-cache (%.0f с)" % age
    cf = _arch_cf(bridge, pid, "mcp-formxml-%d-%s.cf" % (os.getpid(), _stamp()))
    _cf_registry_put(base, cf)
    return cf, "fresh"


def _form_record_sha1(cf: Path, form_uuid: str) -> str:
    """Хеш записи формы в cf (неизменяемый снимок) — ключ XML-кэша М2."""
    from cf_inspect import CfFile
    key = form_uuid.strip("{}").lower() + ".0"
    with CfFile(str(cf)) as c:
        data = c.read({r["name"]: r for r in c.records()}[key])
    return hashlib.sha1(data).hexdigest()


def form_get_xml(session: int, obj_path: str, form_name: str, owner_path: str,
                 cf_path: str | None = None, base_dir: Path | None = None,
                 temp_base: Path | None = None, cf_max_age_s: float = 0) -> dict:
    """§3.1: XML формы из cf последнего сохранённого состояния.

    obj_path — владелец («Справочник.Номенклатура» или «ОбщаяФорма.X»);
    form_name — имя формы («ФормаЭлемента», «» для самой общей формы);
    owner_path — путь владельца для снимка К2 (для общей формы = сам объект);
    temp_base — готовая temp-база из этого же cf (иначе строится в work/tread);
    cf_max_age_s — переиспользовать архив cf этого основания, если он моложе
    (0 = всегда снимать свежий; cf страдает 129 с на 2 ГБ).
    Кэш М2: XML ищется по хешу записи формы в cf — hit возвращает без temp_read
    (записи cf неизменяемы, инвалидация не нужна); temp_read переиспользуется
    для того же cf (маркер размер+mtime).
    """
    t0 = time.time()
    cfg = load_config()
    bridge = Bridge(cfg)
    base = _session_base(bridge, session)
    pid = session_for_base(bridge, base)
    if _modified(bridge, pid):
        raise FormXmlError(
            "контейнер изменён (isModified=true) — есть несохранённые правки; "
            "чтение отражает только сохранённое состояние (§3.1, К1)")
    snap = snapshot_owner(bridge, pid, owner_path)
    form_uuid = _form_uuid(bridge, pid, obj_path, form_name)
    cf, cf_src = _get_cf(bridge, pid, base, cf_path, cf_max_age_s)
    record_hash = _form_record_sha1(cf, form_uuid)
    xml_cache = CACHE / "xml" / (record_hash + ".xml")
    if xml_cache.is_file():
        result = {"xml": xml_cache.read_text(encoding="utf-8-sig"),
                  "xml_path": str(xml_cache), "cf": str(cf), "cf_source": cf_src,
                  "snapshot": snap, "full_name": obj_path if not form_name else f"{obj_path}.Форма.{form_name}",
                  "form_uuid": form_uuid, "record_sha1": record_hash,
                  "cache": "xml-hit", "ms": round((time.time() - t0) * 1000)}
        result["gc"] = _gc_housekeeping(None, cf)
        return result
    work = (base_dir or WORK) / _cf_key(cf)
    work.mkdir(parents=True, exist_ok=True)
    tread = temp_base if temp_base is not None else work / "tread"
    reusable = temp_base is None and _tread_reusable(work, cf)
    if temp_base is None and not reusable:
        _mockup_init(tread, cf)
        _write_tread_marker(work, cf)
    full_name = obj_path if not form_name else f"{obj_path}.Форма.{form_name}"
    lf = work / "dump.list"
    lf.write_text(full_name + "\n", encoding="utf-8")
    dump_dir = work / "xml"
    if dump_dir.exists():
        shutil.rmtree(dump_dir)
    _designer(["/F", str(tread), "/DumpConfigToFiles", str(dump_dir),
               "-listFile", str(lf)], work / "dump.log", work / "dump.res")
    xmls = sorted(dump_dir.rglob("Form.xml"))
    if len(xmls) != 1:
        raise FormXmlError(f"ожидался Form.xml, найдено {len(xmls)}: {xmls}")
    xml = xmls[0].read_text(encoding="utf-8-sig")
    xml_cache.parent.mkdir(parents=True, exist_ok=True)
    xml_cache.write_text(xml, encoding="utf-8")
    return {"xml": xml, "xml_path": str(xmls[0]), "cf": str(cf), "cf_source": cf_src,
            "snapshot": snap, "full_name": full_name, "form_uuid": form_uuid,
            "record_sha1": record_hash, "cache": "temp-reuse" if reusable else "miss",
            "gc": _gc_housekeeping(work, cf),
            "ms": round((time.time() - t0) * 1000)}


def form_set_xml(session: int, obj_path: str, form_name: str, owner_path: str,
                 edit, edit_marker: str | None = None,
                 expected_snapshot: dict | None = None,
                 base_dir: Path | None = None) -> dict:
    """§3.2: правка формы через temp_write + /LoadCfg в живую базу.

    edit — callable(xml_text) -> новый xml_text (правка вызывающим; валидация —
    скиллом form-validate до вызова); edit_marker — подстрока-признак правки для
    постусловия М5 (имя добавленного элемента и т.п.). Запись в живую базу —
    штатной командой «Загрузить конфигурацию из файла» через мост: конфигуратор
    остаётся открытым, сессия не меняется (new_session = session).
    """
    t0 = time.time()
    cfg = load_config()
    bridge = Bridge(cfg)
    pid = session_for_base(bridge, _session_base(bridge, session))
    if _modified(bridge, pid):
        raise FormXmlError("контейнер изменён — сначала сохраните или отклоните правки")
    snap = snapshot_owner(bridge, pid, owner_path)
    if expected_snapshot is not None and snap != expected_snapshot:
        raise FormXmlError(
            "состав владельца изменился с момента чтения XML (К2): "
            f"было {expected_snapshot}, стало {snap} — отказ до записи")
    form_uuid = _form_uuid(bridge, pid, obj_path, form_name)
    cf = _arch_cf(bridge, pid, "mcp-formxml-%d-%s.cf" % (os.getpid(), _stamp()))
    work = (base_dir or WORK) / cf.stem
    work.mkdir(parents=True, exist_ok=True)
    twrite = work / "twrite"
    _mockup_init(twrite, cf)
    full_name = obj_path if not form_name else f"{obj_path}.Форма.{form_name}"
    lf = work / "dump.list"
    lf.write_text(full_name + "\n", encoding="utf-8")
    dump_dir = work / "xml0"
    _designer(["/F", str(twrite), "/DumpConfigToFiles", str(dump_dir),
               "-listFile", str(lf)], work / "dump0.log", work / "dump0.res")
    form_xml = next(dump_dir.rglob("Form.xml"))
    src_text = form_xml.read_text(encoding="utf-8-sig")
    edited = edit(src_text)
    if edited == src_text:
        raise FormXmlError("правка не изменила XML — отказ до цепочки (иначе минуты впустую)")
    _validate_form_xml(edited)
    load_dir = work / "load"
    if load_dir.exists():
        shutil.rmtree(load_dir)
    rel = form_xml.relative_to(dump_dir)
    (load_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (load_dir / rel).write_text(edited, encoding="utf-8-sig")
    lfl = work / "load.list"
    lfl.write_text(str(rel).replace("/", "\\") + "\n", encoding="utf-8")
    _designer(["/F", str(twrite), "/LoadConfigFromFiles", str(load_dir),
               "-listFile", str(lfl)], work / "load.log", work / "load.res")
    # Постусловие М5: платформа переформатирует XML при загрузке (канон!), поэтому
    # побайтовое равенство проверяется между ДВУМЯ ВЫГРУЗКАМИ платформы + признак
    # правки должен присутствовать (иначе загрузка не сработала — отказ до /DumpCfg).
    def _dump_form(tag):
        d = work / ("xml" + tag)
        _designer(["/F", str(twrite), "/DumpConfigToFiles", str(d),
                   "-listFile", str(lf)], work / f"dump{tag}.log", work / f"dump{tag}.res")
        return next(d.rglob("Form.xml")).read_text(encoding="utf-8-sig")
    canon1 = _dump_form("1")
    if edit_marker and edit_marker not in canon1:
        raise FormXmlError("postcondition М5: правки нет в re-dump — загрузка не сработала")
    (load_dir / rel).write_text(canon1, encoding="utf-8-sig")  # канон вместо рукописи
    _designer(["/F", str(twrite), "/LoadConfigFromFiles", str(load_dir),
               "-listFile", str(lfl)], work / "load2.log", work / "load2.res")
    canon2 = _dump_form("2")
    if canon1 != canon2:
        raise FormXmlError("postcondition М5: выгрузки платформы не идемпотентны")
    cf2 = work / "cf2.cf"
    _designer(["/F", str(twrite), "/DumpCfg", str(cf2)],
              work / "dumpcf.log", work / "dumpcf.res")
    # запись в живую базу — штатной командой «Загрузить конфигурацию из файла»
    # (Config, 6) через мост: конфигуратор ОСТАЁТСЯ ОТКРЫТЫМ. Пакетный путь
    # (kill + /LoadCfg) упирался в «1Cv8.cdn. Схема не зарегистрирована» — окно
    # недоступности базы после закрытия + зомби-конфигураторы (e2e 28.10.2026);
    # мостовая загрузка снимает весь класс проблем, сессия не меняется.
    bridge.call(pid, "cfg_load_cf", {"cf": str(cf2)}, timeout=600)
    try:
        bridge.call(pid, "update_db_cfg",
                    {"own_bases": cfg.own_bases,
                     "allowedDialogs": cfg.update_db_allowed_dialogs}, timeout=600)
    except BridgeError as e:
        # «недоступна» = конфигурация БД уже соответствует cf2 (структура та же)
        if "недоступна" not in str(e):
            raise
    new_pid = pid
    # верификация: свежий cf живой базы, текст формы == cf2 (нормализованные)
    from cf_inspect import CfFile
    from form_text import texts_equal, load_form_text
    key = form_uuid.strip("{}").lower() + ".0"
    cfv = _arch_cf(bridge, new_pid, "mcp-formxml-%d-%s-v.cf" % (os.getpid(), _stamp()))
    with CfFile(str(cfv)) as c:
        cfv_text = c.read({r["name"]: r for r in c.records()}[key]).decode("utf-8-sig")
    with CfFile(str(cf2)) as c:
        cf2_text = c.read({r["name"]: r for r in c.records()}[key]).decode("utf-8-sig")
    if not texts_equal(cfv_text, cf2_text):
        raise FormXmlError("верификация: cf живой базы ≠ cf2 по тексту формы")
    # (б) живой редактор: form_save содержит признак правки (§3.2, дёшево — без круга)
    if edit_marker:
        live = bridge.call(new_pid, "form_save",
                           {"path": f"{obj_path}.Форма.{form_name}" if form_name else obj_path,
                            "prop": "Форма", "file": "formxml-verify.txt"}, timeout=120)
        live_text = load_form_text(live["file"])
        if edit_marker not in live_text:
            raise FormXmlError("верификация (б): правки нет в живом form_save")
    return {"cf2": str(cf2), "verify_cf": str(cfv), "new_session": new_pid,
            "form_uuid": form_uuid, "gc": _gc_housekeeping(work, cfv),
            "ms": round((time.time() - t0) * 1000)}


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _session_base(bridge: Bridge, session: int) -> Path:
    for s in bridge.sessions():
        if s.pid == session:
            conn = str(s.data.get("conn") or "")
            value = conn.split("=", 1)[1].rstrip(";\" ")
            return Path(value)
    raise FormXmlError(f"сессия {session} не найдена")
