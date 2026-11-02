"""Handoff «живая база -> cf -> база линии» (этап 12, шаг Л1) и доставка cfe
расширения в базу линии (этап 16).

Режим 1 (правки живой базы):
python server/tools/mcp_to_line.py --live-base <путь> --line-base <путь> \
    --objects "Справочник.Х,Справочник.Х.Реквизит.У" [--dir <verify>] [--timeout 1800]

Режим 2 (готовый cfe расширения, этап 16 — замыкает цикл build_ext → линия):
python server/tools/mcp_to_line.py --cfe <файл.cfe> --ext-name <Имя> \
    --line-base <путь> [--dir <verify>] [--timeout 1800]

Шаги (каждая команда конфигуратора — ОТДЕЛЬНЫЙ вызов процесса; склейка в одном
вызове зависала намертво, находка П2 этапа 12):
  1. Живая сессия: конфигурация должна быть сохранена (isModified=false);
     при изменениях — config_save с автоархивом cf (точка отката).
  2. cf живой базы: cfg_archive_cf (штатная команда, путь через onSelectFile).
  3. База линии (пакетный DESIGNER): /LoadCfg cf -> /UpdateDBCfg ->
     /DumpConfigToFiles -listFile. Вердикт каждого шага: код возврата + /DumpResult
     + отсутствие фатальных фраз в логе + постусловие (res-файл/выгрузка).
  4. Постусловие: каждый объект из --objects присутствует в выгрузке.

Режим 2: /LoadCfg <cfe> -Extension <Имя> -> /CheckModules -Extension ->
/UpdateDBCfg -Extension -> /DumpCfg <копия> -Extension; постусловие — записи cfe
и снятой копии совпадают по хешам (cf_inspect; схема П14-5, «несуществующее имя в
-Extension — молчаливый пропуск rc=0» отсечено именно постусловием).

База-макет — только непривязанная (на привязанной Insert/Delete не работают,
25.10.2026) и чистая: cf увозит ВСЮ её конфигурацию.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_snegopat.bridge import BridgeError, norm_file_key, platform_supported  # noqa: E402
from mcp_snegopat.config import load_config  # noqa: E402
from mcp_snegopat.server import Toolkit  # noqa: E402

FATAL_PHRASES = ("Ошибка при ", "не выполнена", "Отказано в доступе")
PLATFORM = Path(r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe")


def designer_verdict(rc: int, res_text: str, log_text: str) -> tuple[bool, str]:
    """Вердикт одного designer-вызова: код + /DumpResult + фатальные фразы.
    Ревью M6: отсутствующий/пустой /DumpResult — ОТКАЗ (не успех): молчаливые
    пропуски пакетных команд известны (несуществующее имя в -Extension)."""
    if rc != 0:
        return False, f"код возврата {rc}" + (f"; лог: {log_text[-200:]}" if log_text else "")
    if res_text.lstrip("﻿") != "0":
        if not res_text:
            return False, "/DumpResult отсутствует (файл не создан или пуст)"
        return False, f"/DumpResult = {res_text}"
    for phrase in FATAL_PHRASES:
        if phrase in log_text:
            return False, f"фатальная фраза в логе: {phrase!r}"
    return True, "ok"


def run_designer(args: list[str], out_log: Path, res_file: Path,
                 timeout: float) -> tuple[bool, str]:
    """Один вызов 1cv8 DESIGNER. Вердикт: код + DumpResult + лог + постусловие —
    у вызывающего (правило 'вердикт инструмента — не факт').

    /DisableStartupDialogs обязателен: без него пакетная команда на ошибке
    (-listFile с несуществующим объектом и т.п.) поднимает МОДАЛЬНОЕ окно
    «Объект метаданных … не существует» и висит вечно (24.10.2026).
    По таймауту процесс УБИВАЕТСЯ: subprocess.call(timeout) процесс не снимает
    (ревью 25.10.2026) — остался бы живой 1cv8, держащий базу линии."""
    out_log.parent.mkdir(parents=True, exist_ok=True)
    res_file.unlink(missing_ok=True)
    cmd = [str(PLATFORM), "DESIGNER", "/DisableStartupDialogs"] + args \
        + ["/Out", str(out_log), "/DumpResult", str(res_file)]
    p = subprocess.Popen(cmd)
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        return False, f"таймаут {timeout:.0f} с (процесс снят)"
    res = res_file.read_text(encoding="utf-8-sig", errors="replace").strip() \
        if res_file.is_file() else ""
    log = out_log.read_text(encoding="utf-8-sig", errors="replace") if out_log.is_file() else ""
    return designer_verdict(rc, res, log)


def session_for_base(tk: Toolkit, live_base: Path) -> int:
    """Живая сессия именно этой базы (нормализация как normFilePath моста:
    / -> \\, нижний регистр, без хвостовых \\) + гейт платформы."""
    want = norm_file_key(live_base)
    for s in tk.bridge.sessions():
        if not s.alive:
            continue
        if not platform_supported(s.data.get("platform")):
            continue
        conn = str(s.data.get("conn") or "")
        if "=" not in conn:
            continue
        value = conn.split("=", 1)[1].rstrip(";\" ")
        if norm_file_key(Path(value)) == want:
            return s.pid
    raise SystemExit(
        f"нет живой сессии конфигуратора на {live_base}; откройте конфигуратор "
        "(starter.exe + 1cv8.exe без /DisableStartupDialogs)")


def check_args(a: argparse.Namespace) -> None:
    """Валидация аргументов (этап 16: два режима, взаимно исключающие)."""
    if not a.live_base and not a.cfe:
        raise SystemExit("нужен один из --live-base (режим cf) или --cfe (режим расширения)")
    if a.cfe and a.live_base:
        raise SystemExit("--cfe и --live-base взаимоисключающие")
    if a.cfe:
        if not a.ext_name:
            raise SystemExit("--cfe требует --ext-name")
        if not Path(a.cfe).is_file() or Path(a.cfe).stat().st_size < 100:
            raise SystemExit(f"--cfe не найден или пуст: {a.cfe}")
        if a.objects:
            raise SystemExit("--objects в режиме --cfe не используется (постусловие — хеши записей)")
        return
    if not a.objects:
        raise SystemExit("--objects обязателен в режиме --live-base")
    objects = [o.strip() for o in a.objects.split(",") if o.strip()]
    if not objects:
        raise SystemExit("--objects пуст")
    # выгрузка именует файлы ПО ИМЕНИ объекта верхнего уровня (2 сегмента):
    # подчинённые (реквизиты и т.п.) отдельных файлов не получают — постусловие
    # по ним всегда было бы ложным FAIL (ревью 25.10.2026)
    for obj in objects:
        if len(obj.split(".")) != 2:
            raise SystemExit(f"--objects: «{obj}» — ожидается объект верхнего уровня "
                             "вида «Вид.Имя»; подчинённые проверяйте чтением XML родителя")


def cfe_records_equal(p1: Path, p2: Path) -> tuple[bool, str]:
    """Постусловие доставки cfe (этап 16, схема П14-5): наборы записей и их хеши.
    Counter пар (имя, хеш) — ревью L10: словарь по имени терял бы дубликаты имён.
    Несуществующее имя в -Extension даёт МОЛЧАЛИВЫЙ пропуск rc=0 — отсекается здесь."""
    from collections import Counter
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cf_inspect import CfFile  # noqa: E402
    import hashlib  # noqa: E402

    def inv(p: Path) -> Counter:
        with CfFile(str(p)) as cf:
            return Counter((r["name"], hashlib.sha1(cf.read(r)).hexdigest())
                           for r in cf.records())
    a, b = inv(p1), inv(p2)
    if a == b:
        return True, "записей %d, байт-в-байт" % sum(a.values())
    diff = sorted(k for k in set(a) | set(b) if a[k] != b[k])
    return False, "записей %d/%d, расходятся: %s" % (sum(a.values()), sum(b.values()),
                                                     [d[0][:16] for d in diff[:6]])


def cfe_delivery(a: argparse.Namespace, line_base: Path, work: Path) -> int:
    """Режим 2 (этап 16): готовый cfe → база линии. Последовательные вызовы
    (НЕ склейкой); постусловие — записи cfe == записи снятой копии расширения
    (молчаливый пропуск несуществующего имени в -Extension отсекается здесь)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    results: list[dict] = []

    def step(name: str, ok: bool, info: str) -> bool:
        results.append({"step": name, "ok": ok, "info": info})
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {info}", flush=True)
        return ok

    cfe = Path(a.cfe)
    step("cfe", cfe.is_file(), f"{cfe} {cfe.stat().st_size} байт")

    delivered = work / f"delivered-{a.ext_name}.cfe"
    delivered.unlink(missing_ok=True)  # прошлый прогон дал бы ложное «ок» постусловия
    runs = [
        ("LoadCfgExt", ["/LoadCfg", str(cfe), "-Extension", a.ext_name]),
        ("CheckModulesExt", ["/CheckModules", "-Extension", a.ext_name]),
        ("UpdateDBCfgExt", ["/UpdateDBCfg", "-Extension", a.ext_name]),
        ("DumpCfgExt", ["/DumpCfg", str(delivered), "-Extension", a.ext_name]),
    ]
    for name, args in runs:
        ok, info = run_designer(["/F", str(line_base)] + args,
                                work / f"{name}-{stamp}.log", work / f"{name}-{stamp}.res",
                                a.timeout)
        if not step(f"база линии: {name}", ok, info):
            return finish(results, work, stamp)
        if name == "CheckModulesExt":
            log = (work / f"{name}-{stamp}.log").read_text(encoding="utf-8-sig",
                                                           errors="replace") \
                if (work / f"{name}-{stamp}.log").is_file() else ""
            ok_log = "не обнаружено" in log or "Синтаксических ошибок" in log
            if not step("CheckModules: синтаксис", ok_log, log[-160:]):
                return finish(results, work, stamp)

    if not delivered.is_file() or delivered.stat().st_size == 0:
        step("постусловие: копия расширения", False,
             f"{delivered} не создан (расширение не загрузилось?)")
        return finish(results, work, stamp)
    ok, info = cfe_records_equal(cfe, delivered)
    step("постусловие: записи cfe == копии", ok, info)
    return finish(results, work, stamp)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live-base", default="", help="режим 1: непривязанная база-макет с правками")
    ap.add_argument("--cfe", default="", help="режим 2: готовый cfe расширения")
    ap.add_argument("--ext-name", default="", help="режим 2: имя расширения для -Extension")
    ap.add_argument("--line-base", required=True, help="база линии (закрыта, не привязана)")
    ap.add_argument("--objects", default="",
                    help="режим 1: объекты постусловия через запятую (имена для -listFile)")
    ap.add_argument("--dir", default=r"Y:\Snegopat-mcp\verify\mcp_to_line",
                    help="рабочий каталог выгрузок/логов")
    ap.add_argument("--timeout", type=float, default=1800, help="лимит одного вызова, с")
    a = ap.parse_args()
    check_args(a)

    line_base = Path(a.line_base)
    work = Path(a.dir)
    work.mkdir(parents=True, exist_ok=True)

    if a.cfe:
        return cfe_delivery(a, line_base, work)
    live_base = Path(a.live_base)
    objects = [o.strip() for o in a.objects.split(",") if o.strip()]

    tk = Toolkit(load_config())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    results: list[dict] = []

    def step(name: str, ok: bool, info: str) -> bool:
        results.append({"step": name, "ok": ok, "info": info})
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {info}", flush=True)
        return ok

    # 1. живая сессия и сохранность
    pid = session_for_base(tk, live_base)
    tk.bridge.call(pid, "config_open", {})
    si = tk.bridge.call(pid, "session_info", {})
    cont = next((c for c in si.get("containers", [])
                 if "Основная" in str(c.get("identifier"))), None)
    if not step("живая сессия", cont is not None,
                f"pid {pid}, контейнеров {len(si.get('containers', []))}"):
        return finish(results, work, stamp)
    if cont and cont.get("isModified"):
        try:
            tk.config_save(session=pid)
            step("config_save", True, "были изменения — сохранено (архив cf снят)")
        except BridgeError as e:
            step("config_save", False, str(e)[:200])
            return finish(results, work, stamp)
    else:
        step("config_save", True, "изменений нет (isModified=false)")

    # 2. cf живой базы
    try:
        arch = tk.cfg_archive_cf(session=pid)
    except BridgeError as e:
        step("cf живой базы", False, str(e)[:200])
        return finish(results, work, stamp)
    cf = Path(arch["file"])
    step("cf живой базы", cf.is_file() and cf.stat().st_size > 1000,
         f"{cf} {arch.get('size')} байт, {arch.get('ms')} мс")

    # 3. база линии: три последовательных вызова (НЕ склейкой)
    lf = work / "objects.txt"
    lf.write_text("\n".join(objects) + "\n", encoding="utf-8")
    dump_dir = work / "dump"
    if dump_dir.is_dir():
        shutil.rmtree(dump_dir)  # файлы прошлых прогонов давали ложное «ок» постусловия
    runs = [
        ("LoadCfg", ["/LoadCfg", str(cf)]),
        ("UpdateDBCfg", ["/UpdateDBCfg"]),
        ("DumpConfigToFiles", ["/DumpConfigToFiles", str(dump_dir), "-listFile", str(lf),
                               "-Format", "Hierarchical"]),
    ]
    for name, args in runs:
        ok, info = run_designer(["/F", str(line_base)] + args,
                                work / f"{name}.log", work / f"{name}.res", a.timeout)
        if not step(f"база линии: {name}", ok, info):
            return finish(results, work, stamp)

    # 4. постусловие: объекты в выгрузке
    dumped = {p.stem for p in dump_dir.rglob("*.xml")} if dump_dir.is_dir() else set()
    for obj in objects:
        leaf = obj.split(".")[-1]
        step(f"постусловие: {obj}", leaf in dumped, f"выгрузка содержит: {sorted(dumped)[:8]}")

    return finish(results, work, stamp)


def finish(results: list[dict], work: Path, stamp: str) -> int:
    failed = [r for r in results if not r["ok"]]
    out = work / f"handoff-{stamp}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nитог: провалено {len(failed)} из {len(results)}"
          f" ({'ЕСТЬ ПРОВАЛЫ' if failed else 'ВСЁ ОК'}); отчёт {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
