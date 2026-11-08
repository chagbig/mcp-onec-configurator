# -*- coding: utf-8 -*-
"""doctor — самодиагностика установки mcp-configurator.

Один вызов проверяет цепочку «платформа → стартер → дом → флаг → конфиг →
DWX → журнал → живая сессия» и выдаёт вердикт с подсказками. Используется:
  - как CLI: python doctor.py --home C:\\Snegopat-mcp (когда MCP не отвечает);
  - как MCP-инструмент doctor (server.py импортирует run_filesystem_checks
    и добавляет живо-сессионные проверки).

Статусы: ok / warn (работает, но аномалия) / fail (не работает).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PLATFORM_CANDIDATES = [
    r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe",
]
DWX_CLSID = "{89565276-A714-4a43-912E-978B935EDCCC}"


def _check(name: str, status: str, detail: str = "", hint: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail, "hint": hint}


def check_platform(home: Path) -> dict:
    for p in PLATFORM_CANDIDATES:
        if Path(p).is_file():
            return _check("платформа 8.3.27.2074", "ok", p)
    return _check("платформа 8.3.27.2074", "fail",
                  "не найдена " + PLATFORM_CANDIDATES[0],
                  "установите платформу 8.3.27.2074 (мост стартует только на ней)")


def check_starter() -> dict:
    try:
        # tasklist на русской Windows пишет в cp866; text=True с utf-8 падает
        raw = subprocess.run(["tasklist", "/FI", "IMAGENAME eq starter.exe"],
                             capture_output=True, timeout=30).stdout or b""
        out = raw.decode("cp866", errors="replace")
    except Exception as e:
        return _check("стартер", "warn", f"не удалось выполнить tasklist: {e}",
                      "проверьте процесс starter.exe вручную")
    if "starter.exe" in out:
        return _check("стартер", "ok", "процесс starter.exe запущен")
    return _check("стартер", "fail", "starter.exe не запущен — мост не внедрится",
                  "запустите starter.exe из <снегопат>\\x64 (cwd = x64)")


def check_home(home: Path) -> tuple[dict, dict, dict]:
    marker = home / "mcp" / "mcp_bridge.home"
    if marker.is_file():
        m1 = _check("дом (маркер mcp_bridge.home)", "ok", str(marker))
    else:
        m1 = _check("дом (маркер mcp_bridge.home)", "fail",
                    f"нет {marker}",
                    "создайте пустой файл mcp_bridge.home в <дом>\\mcp\\ или "
                    "переустановите (install.ps1)")
    flag = home / "mcp" / "mcp_bridge.on"
    if flag.is_file():
        m2 = _check("флаг включения mcp_bridge.on", "ok", str(flag))
    else:
        m2 = _check("флаг включения mcp_bridge.on", "fail", f"нет {flag}",
                    "создайте пустой файл mcp_bridge.on — без него мост не ставит таймер")
    m3 = _check("каталоги моста", "ok" if (home / "mcp" / "sessions").is_dir()
                else "warn", str(home / "mcp"))
    return m1, m2, m3


def check_config(home: Path) -> tuple[dict, dict]:
    cfg = home / "mcp" / "mcp_snegopat.json"
    if not cfg.is_file():
        return (_check("конфиг сервера", "fail", f"нет {cfg}",
                       "создайте из mcp_snegopat.json.example"),
                _check("own_bases", "warn", "конфига нет — базы не проверены"))
    import json
    try:
        data = json.loads(cfg.read_text(encoding="utf-8-sig"))
    except Exception as e:
        return (_check("конфиг сервера", "fail", f"не читается: {e}"),
                _check("own_bases", "warn", "конфиг не читается"))
    c1 = _check("конфиг сервера", "ok", str(cfg))
    bases = data.get("own_bases") or []
    if not bases:
        c2 = _check("own_bases", "warn", "пусто — запись в базы запрещена гейтом",
                    'допишите свои базы: File="<путь>";')
    else:
        missing = []
        for b in bases:
            m = re.search(r'File="([^"]+)"', b)
            if m and not Path(m.group(1)).is_dir():
                missing.append(m.group(1))
        c2 = (_check("own_bases", "warn",
                     "каталоги баз не найдены: " + "; ".join(missing),
                     "исправьте пути в own_bases")
              if missing else
              _check("own_bases", "ok", f"{len(bases)} баз(ы)"))
    return c1, c2


def check_dwx() -> dict:
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Classes\CLSID" + "\\" + DWX_CLSID + r"\InprocServer32")
        dll = winreg.QueryValueEx(k, "")[0]
        ok = Path(dll).is_file()
        return _check("DynamicWrapperX", "ok" if ok else "fail",
                      dll + ("" if ok else " (файл отсутствует)"),
                      "" if ok else "переустановите: regsvr32 /i <дом>\\bin\\dynwrapx.dll")
    except OSError:
        return _check("DynamicWrapperX", "fail", "не зарегистрирован в HKCU",
                      "regsvr32 /i <дом>\\bin\\dynwrapx.dll (без админа)")


def check_journal(home: Path, enabled: bool = True) -> dict:
    jdir = home / "mcp" / "journal"
    if not enabled:
        return _check("журнал", "warn", "выключен (journal_configure enabled=false)")
    files = sorted(jdir.glob("*.jsonl")) if jdir.is_dir() else []
    total = sum(f.stat().st_size for f in files) if files else 0
    return _check("журнал", "ok", f"{len(files)} файл(ов), {total} байт",
                  "" if files else "записей пока нет — появится после первой правки")


def check_load_marker(home: Path) -> dict:
    p = home / "mcp" / "mcp_bridge-load.txt"
    if p.is_file():
        return _check("маркер загрузки моста", "ok",
                      p.read_text(encoding="utf-8", errors="replace").strip()[:120])
    return _check("маркер загрузки моста", "warn",
                  "нет mcp_bridge-load.txt — конфигуратор не перезапускался после "
                  "установки/обновления моста",
                  "откройте конфигуратор (мост грузится на старте)")


def run_filesystem_checks(home: Path, journal_enabled: bool = True,
                          with_starter: bool = True) -> dict:
    """Полный файловый чек-лист (без живой сессии). {verdict, home, checks}.
    with_starter=False — для тестов (наличие процесса не файловый факт)."""
    home = Path(home)
    checks = [check_platform(home)]
    if with_starter:
        checks.append(check_starter())
    m1, m2, m3 = check_home(home)
    checks += [m1, m2, m3]
    c1, c2 = check_config(home)
    checks += [c1, c2]
    checks.append(check_dwx())
    checks.append(check_journal(home, journal_enabled))
    checks.append(check_load_marker(home))
    verdict = ("fail" if any(c["status"] == "fail" for c in checks) else
               "warn" if any(c["status"] == "warn" for c in checks) else "ok")
    return {"verdict": verdict, "home": str(home), "checks": checks}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Самодиагностика mcp-configurator")
    ap.add_argument("--home", default="",
                    help="дом установки (иначе из env MCP_SNEGOPAT_HOME)")
    a = ap.parse_args(argv)
    home = a.home or os.environ.get("MCP_SNEGOPAT_HOME", r"C:\Snegopat-mcp")
    res = run_filesystem_checks(Path(home))
    print("ДОМ:", res["home"], "| ВЕРДИКТ:", res["verdict"].upper())
    for c in res["checks"]:
        mark = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}.get(c["status"], "??  ")
        print(f"  [{mark}] {c['name']}: {c['detail']}")
        if c["hint"]:
            print(f"         подсказка: {c['hint']}")
    return 0 if res["verdict"] != "fail" else 1


if __name__ == "__main__":
    sys.exit(main())
