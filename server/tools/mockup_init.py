"""Инициализация чистой базы-макета (этап 12, шаг Л3).

python server/tools/mockup_init.py --base <путь> [--from <cf>] [--reset]

cf живой базы увозит ВСЮ её конфигурацию (этап 9, этап 12/П3), поэтому база-макет
должна быть чистой: пересоздаётся из источника, а не накапливает опыты.

Режимы:
  без --from — пустая база (CREATEINFOBASE), структура проектируется с нуля через MCP;
  --from <cf> — пустая база + /LoadCfg cf + /UpdateDBCfg (последовательно, каждая
  команда — отдельный вызов; склейка висит — этап 12/П2), например из эталонного cf.

--reset разрешает удалить существующий каталог базы; без него существующая база
не трогается (защита от потери работы агента).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

PLATFORM = Path(r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe")


def run_designer(args: list[str], work: Path, name: str, timeout: float = 7200) -> bool:
    """Один вызов DESIGNER с /DisableStartupDialogs (модалки ошибок — Л1, этап 12).
    Таймаут 7200: UpdateDBCfg боевого cf (~2 ГБ) идёт ~20-30 мин (скил моста)."""
    log = work / f"mockup-{name}.log"
    res = work / f"mockup-{name}.res"
    res.unlink(missing_ok=True)
    cmd = [str(PLATFORM), "DESIGNER", "/DisableStartupDialogs"] + args \
        + ["/Out", str(log), "/DumpResult", str(res)]
    try:
        rc = subprocess.call(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    if rc != 0:
        return False
    out = res.read_text(encoding="utf-8-sig").strip() if res.is_file() else ""
    return out.lstrip("﻿") == "0"


def create_empty(base: Path, work: Path) -> bool:
    try:
        subprocess.run(
            [str(PLATFORM), "CREATEINFOBASE", f"FILE={base}"],
            timeout=300, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except (subprocess.SubprocessError, OSError):
        return False
    return (base / "1Cv8.1CD").is_file()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", required=True, help="каталог базы-макета")
    ap.add_argument("--from", dest="src_cf", help="эталонный cf (необязательно)")
    ap.add_argument("--reset", action="store_true",
                    help="разрешить пересоздать существующую базу")
    ap.add_argument("--dir", default=r"Y:\Snegopat-mcp\verify\mockup",
                    help="каталог логов")
    a = ap.parse_args()

    base = Path(a.base)
    work = Path(a.dir)
    work.mkdir(parents=True, exist_ok=True)

    if a.src_cf:
        src = Path(a.src_cf)
        if not src.is_file() or src.stat().st_size < 1000:
            raise SystemExit(f"cf не найден или пуст: {src}")  # ДО удаления старого макета
    if base.exists() and any(base.iterdir()):
        if not a.reset:
            raise SystemExit(f"{base} существует и не пуст; для пересоздания — --reset "
                             "(несохранённые правки агента будут потеряны)")
        # rmtree только по признакам базы 1С (ревью 25.10.2026): опечатка в --base
        # не должна стоить произвольного каталога
        markers = [p.name for p in base.iterdir() if p.name in ("1Cv8.1CD", "1Cv8tmp.1CD", "1cv8ddb.1CD")]
        if not markers:
            raise SystemExit(f"{base} не выглядит базой 1С (нет 1Cv8.1CD) — --reset отказался удалять")
        shutil.rmtree(base)
    if base.exists():
        raise SystemExit(f"{base} занят и не удаляется — закрыт ли конфигуратор?")

    t0 = time.time()
    print(f"создание пустой базы {base}...", flush=True)
    if not create_empty(base, work):
        raise SystemExit("CREATEINFOBASE не удался (путь? права?)")
    print(f"  ok ({time.time()-t0:.1f} с)")

    print(f"загрузка {src.name} ({src.stat().st_size} байт)...", flush=True)
    for name, args in (
        ("load", ["/F", str(base), "/LoadCfg", str(src)]),
        ("upd", ["/F", str(base), "/UpdateDBCfg"]),
    ):
        ok = run_designer(args, work, name)
        print(f"  {name}: {'ok' if ok else 'FAIL'}", flush=True)
        if not ok:
            return 1

    print(f"макет готов: {base} ({time.time()-t0:.1f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
