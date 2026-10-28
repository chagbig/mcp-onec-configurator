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


class FormXmlError(Exception):
    pass


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


def form_get_xml(session: int, obj_path: str, form_name: str, owner_path: str,
                 cf_path: str | None = None, base_dir: Path | None = None,
                 temp_base: Path | None = None) -> dict:
    """§3.1: XML формы из cf последнего сохранённого состояния.

    obj_path — владелец («Справочник.Номенклатура» или «ОбщаяФорма.X»);
    form_name — имя формы («ФормаЭлемента», «» для самой общей формы);
    owner_path — путь владельца для снимка К2 (для общей формы = сам объект);
    temp_base — готовая temp-база из этого же cf (иначе строится в work/tread).
    """
    t0 = time.time()
    cfg = load_config()
    bridge = Bridge(cfg)
    pid = session_for_base(bridge, _session_base(bridge, session))
    if _modified(bridge, pid):
        raise FormXmlError(
            "контейнер изменён (isModified=true) — есть несохранённые правки; "
            "чтение отражает только сохранённое состояние (§3.1, К1)")
    snap = snapshot_owner(bridge, pid, owner_path)
    form_uuid = _form_uuid(bridge, pid, obj_path, form_name)
    cf = Path(cf_path) if cf_path else _arch_cf(bridge, pid, "mcp-formxml-" + _stamp() + ".cf")
    work = (base_dir or WORK) / cf.stem
    work.mkdir(parents=True, exist_ok=True)
    tread = temp_base if temp_base is not None else work / "tread"
    if temp_base is None:
        _mockup_init(tread, cf)
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
    return {"xml": xml, "xml_path": str(xmls[0]), "cf": str(cf),
            "snapshot": snap, "full_name": full_name, "form_uuid": form_uuid,
            "ms": round((time.time() - t0) * 1000)}


def form_set_xml(session: int, obj_path: str, form_name: str, owner_path: str,
                 edit, edit_marker: str | None = None,
                 expected_snapshot: dict | None = None,
                 base_dir: Path | None = None) -> dict:
    """§3.2: правка формы через temp_write + /LoadCfg в живую базу.

    edit — callable(xml_text) -> новый xml_text (правка вызывающим; валидация —
    скиллом form-validate до вызова); edit_marker — подстрока-признак правки для
    постусловия М5 (имя добавленного элемента и т.п.). Конфигуратор живой базы
    будет закрыт и переоткрыт (запись через редактор не работает, П3 §4.3).
    """
    t0 = time.time()
    cfg = load_config()
    bridge = Bridge(cfg)
    base = _session_base(bridge, session)
    pid = session_for_base(bridge, base)
    if _modified(bridge, pid):
        raise FormXmlError("контейнер изменён — сначала сохраните или отклоните правки")
    snap = snapshot_owner(bridge, pid, owner_path)
    if expected_snapshot is not None and snap != expected_snapshot:
        raise FormXmlError(
            "состав владельца изменился с момента чтения XML (К2): "
            f"было {expected_snapshot}, стало {snap} — отказ до записи")
    form_uuid = _form_uuid(bridge, pid, obj_path, form_name)
    cf = _arch_cf(bridge, pid, "mcp-formxml-" + _stamp() + ".cf")
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
    edited = edit(form_xml.read_text(encoding="utf-8-sig"))
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
    # запись в живую базу: конфигуратор(ы) закрываются (isModified=false выше).
    # Если пакетный шаг упал — восстанавливаем сессию: без неё пользователь без
    # конфигуратора, а база при отказе /LoadCfg не тронута (загрузка атомарна).
    try:
        _kill_session(bridge, pid, base)
        _designer_retry(["/F", str(base), "/LoadCfg", str(cf2)],
                        work / "loadlive.log", work / "loadlive.res")
        _designer_retry(["/F", str(base), "/UpdateDBCfg"],
                        work / "updlive.log", work / "updlive.res")
    except FormXmlError:
        try:
            _relaunch(base)
        except FormXmlError:
            pass  # исходная ошибка важнее
        raise
    new_pid = _relaunch(base)
    # верификация: свежий cf живой базы, текст формы == cf2 (нормализованные)
    from cf_inspect import CfFile
    from form_text import texts_equal, load_form_text
    key = form_uuid.strip("{}").lower() + ".0"
    cfv = _arch_cf(bridge, new_pid, "mcp-formxml-" + _stamp() + "-v.cf")
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
            "form_uuid": form_uuid, "ms": round((time.time() - t0) * 1000)}


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _session_base(bridge: Bridge, session: int) -> Path:
    for s in bridge.sessions():
        if s.pid == session:
            conn = str(s.data.get("conn") or "")
            value = conn.split("=", 1)[1].rstrip(";\" ")
            return Path(value)
    raise FormXmlError(f"сессия {session} не найдена")


def _kill_session(bridge: Bridge, pid: int, base: Path | None = None) -> None:
    """Закрыть конфигуратор живой базы: сначала мягко (WM_CLOSE, локи снимаются
    самим процессом — жёсткий kill оставляет .cfl и роняет следующий пакетный
    вызов «1Cv8.cdn. Схема не зарегистрирована»), затем форс. Если задан base —
    гасятся ВСЕ конфигураторы этой базы: зомби-сессия от прошлого прогона держит
    1CD и ретраями не лечится (П4 28.10.2026)."""
    targets = {pid}
    if base is not None:
        want = mtl.norm_file_key(base)
        for s in bridge.sessions():
            if not s.alive:
                continue
            conn = str(s.data.get("conn") or "")
            if "=" in conn and mtl.norm_file_key(
                    Path(conn.split("=", 1)[1].rstrip(";\" "))) == want:
                targets.add(s.pid)
    for t in sorted(targets):
        subprocess.run(["taskkill", "/PID", str(t)], capture_output=True)
    for _ in range(25):
        if not any(s.pid in targets and s.alive for s in bridge.sessions()):
            break
        time.sleep(1)
    else:
        for t in sorted(targets):
            subprocess.run(["taskkill", "/PID", str(t), "/F"], capture_output=True)
    for _ in range(30):
        if not any(s.pid in targets and s.alive for s in bridge.sessions()):
            time.sleep(5)  # отпустить файлы базы
            return
        time.sleep(1)
    raise FormXmlError("конфигуратор живой базы не завершился")


def _designer_retry(args, log, res, timeout=1800, attempts=3):
    last = None
    for i in range(attempts):
        try:
            return _designer(args, log, res, timeout)
        except FormXmlError as e:
            last = e
            if "Схема не зарегистрирована" not in str(e) and "1Cv8.cdn" not in str(e):
                raise
            time.sleep(20)  # файлы базы ещё отпускаются после kill
    raise last


def _relaunch(base: Path, timeout=180) -> int:
    """Поднять конфигуратор обратно (без /DisableStartupDialogs — движок Снегопата
    с ним не инициализируется), дождаться моста и открыть конфигурацию."""
    subprocess.Popen([str(PLATFORM), "DESIGNER", "/F", str(base)])
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        cfg = load_config()
        bridge = Bridge(cfg)
        try:
            pid = session_for_base(bridge, base)
        except FormXmlError:
            continue
        bridge.call(pid, "config_open", {}, timeout=300)
        return pid
    raise FormXmlError(f"конфигуратор на {base} не поднялся за {timeout} с")
