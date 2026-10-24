"""Handoff «живая база -> cf -> база линии» (этап 12, шаг Л1).

python server/tools/mcp_to_line.py --live-base <путь> --line-base <путь> \
    --objects "Справочник.Х,Справочник.Х.Реквизит.У" [--dir <verify>] [--timeout 1800]

Шаги (каждая команда конфигуратора — ОТДЕЛЬНЫЙ вызов процесса; склейка в одном
вызове зависала намертво, находка П2 этапа 12):
  1. Живая сессия: конфигурация должна быть сохранена (isModified=false);
     при изменениях — config_save с автоархивом cf (точка отката).
  2. cf живой базы: cfg_archive_cf (штатная команда, путь через onSelectFile).
  3. База линии (пакетный DESIGNER): /LoadCfg cf -> /UpdateDBCfg ->
     /DumpConfigToFiles -listFile. Вердикт каждого шага: код возврата + /DumpResult
     + отсутствие фатальных фраз в логе + постусловие (res-файл/выгрузка).
  4. Постусловие: каждый объект из --objects присутствует в выгрузке.

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

from mcp_snegopat.bridge import BridgeError  # noqa: E402
from mcp_snegopat.config import load_config  # noqa: E402
from mcp_snegopat.server import Toolkit  # noqa: E402

FATAL_PHRASES = ("Ошибка при ", "не выполнена", "Отказано в доступе")
PLATFORM = Path(r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe")


def run_designer(args: list[str], out_log: Path, res_file: Path,
                 timeout: float) -> tuple[bool, str]:
    """Один вызов 1cv8 DESIGNER. Вердикт: код + DumpResult + лог + постусловие —
    у вызывающего (правило 'вердикт инструмента — не факт').

    /DisableStartupDialogs обязателен: без него пакетная команда на ошибке
    (-listFile с несуществующим объектом и т.п.) поднимает МОДАЛЬНОЕ окно
    «Объект метаданных … не существует» и висит вечно (24.10.2026)."""
    out_log.parent.mkdir(parents=True, exist_ok=True)
    res_file.unlink(missing_ok=True)
    cmd = [str(PLATFORM), "DESIGNER", "/DisableStartupDialogs"] + args \
        + ["/Out", str(out_log), "/DumpResult", str(res_file)]
    try:
        rc = subprocess.call(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"таймаут {timeout:.0f} с (процесс снят)"
    if rc != 0:
        return False, f"код возврата {rc}"
    res = res_file.read_text(encoding="utf-8-sig").strip() if res_file.is_file() else ""
    if res and res.lstrip("﻿") != "0":
        return False, f"/DumpResult = {res}"
    log = out_log.read_text(encoding="utf-16-le", errors="replace") if out_log.is_file() else ""
    for phrase in FATAL_PHRASES:
        if phrase in log:
            return False, f"фатальная фраза в логе: {phrase!r}"
    return True, "ok"


def session_for_base(tk: Toolkit, live_base: Path) -> int:
    """Живая сессия именно этой базы (строки соединения сравниваются нормализованно)."""
    def key(p: Path) -> str:
        return re.sub(r"[\";\s]", "", f'File="{p}"'.lower().replace("/", "\\"))

    want = key(live_base)
    for s in tk.bridge.sessions():
        if not s.alive:
            continue
        conn = str(s.data.get("conn") or "")
        if key(Path(conn.split("=", 1)[1].rstrip(";\" "))) == want if "=" in conn else False:
            return s.pid
    raise SystemExit(
        f"нет живой сессии конфигуратора на {live_base}; откройте конфигуратор "
        "(starter.exe + 1cv8.exe без /DisableStartupDialogs)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live-base", required=True, help="непривязанная база-макет с правками")
    ap.add_argument("--line-base", required=True, help="база линии (закрыта, не привязана)")
    ap.add_argument("--objects", required=True,
                    help="объекты постусловия через запятую (имена для -listFile)")
    ap.add_argument("--dir", default=r"Y:\Snegopat-mcp\verify\mcp_to_line",
                    help="рабочий каталог выгрузок/логов")
    ap.add_argument("--timeout", type=float, default=1800, help="лимит одного вызова, с")
    a = ap.parse_args()

    live_base, line_base = Path(a.live_base), Path(a.line_base)
    work = Path(a.dir)
    work.mkdir(parents=True, exist_ok=True)
    objects = [o.strip() for o in a.objects.split(",") if o.strip()]
    if not objects:
        raise SystemExit("--objects пуст")

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
