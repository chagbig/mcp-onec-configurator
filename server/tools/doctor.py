# -*- coding: utf-8 -*-
r"""doctor — самодиагностика установки mcp-configurator.

Один вызов проверяет цепочку «платформа → стартер → дом → флаг → конфиг →
DWX → журнал → живая сессия» и выдаёт вердикт с подсказками. Используется:
  - как CLI: python doctor.py --home C:\\Snegopat-mcp (когда MCP не отвечает);
  - как MCP-инструмент doctor (server.py импортирует run_filesystem_checks
    и добавляет живо-сессионные проверки).

Статусы: ok / warn (работает, но аномалия) / fail (не работает) /
note (нейтрально — не влияет на вердикт; проверка не применима).

№431 (адаптация «аудитор молчаливых фолбэков»): ДРЕЙФ — заявленная
конфигурация != применил рантайм. Две проверки:
  - «дрейф моста»: md5 развёрнутого mcp_bridge.js (все <дом>\snegopat\<вер>\
    и легаси) против канона <репо>\bridge\mcp_bridge.js — deployed-мост может
    отстать от репозитория (install.ps1 синхронизирует, но его могли не звать);
  - «дрейф конфига» (только MCP-блок, где есть живой cfg): runtime-значения
    против файла MCP_SNEGOPAT_CONFIG — конфиг применён до последней правки файла.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

PLATFORM_CANDIDATES = [
    r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe",
]
DWX_CLSID = "{89565276-A714-4a43-912E-978B935EDCCC}"

# №431: канонический мост — относительно doctor.py: server/tools/doctor.py ->
# parents[2] = корень репозитория (deployed-мост мог отстать от репозитория)
REPO_BRIDGE = Path(__file__).resolve().parents[2] / "bridge" / "mcp_bridge.js"
# №431: скалярные поля для сверки runtime-конфига с файлом MCP_SNEGOPAT_CONFIG
# (op_timeouts/long_ops вложенно НЕ читаем — только факт наличия ключа)
DRIFT_SCALARS = ("default_timeout_s", "long_timeout_s", "heartbeat_stale_s",
                 "read_only", "presets",
                 "journal_enabled", "journal_kinds", "journal_max_mb",
                 "journal_keep")
DRIFT_PRESENCE = ("op_timeouts", "long_ops")


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
    if sys.platform != "win32":
        # №431-адаптация: проверка HKCU — Windows-only; на CI/Linux честный
        # note (не fail — Linux-станция не признак сломанной установки)
        return _check("DynamicWrapperX", "note", "проверка только на Windows")
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


def _md5_prefix(path: Path) -> str:
    """Первые 8 символов md5 (не пугаем полными хешами в вердикте)."""
    h = hashlib.md5()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def bridge_deploy_paths(home: Path) -> list[Path]:
    """Все пути развёрнутого моста — как в install.ps1 (шаг «Синхронизация
    моста»): каждая версия под <дом>\\snegopat\\<вер>\\x64\\modules\\snegopat\\
    addins\\mcp\\mcp_bridge.js + легаси-раскладка v1.1.x <дом>\\x64\\..."""
    home = Path(home)
    found: list[Path] = []
    sneg = home / "snegopat"
    if sneg.is_dir():
        for v in sorted(p for p in sneg.iterdir() if p.is_dir()):
            p = v / "x64" / "modules" / "snegopat" / "addins" / "mcp" / "mcp_bridge.js"
            if p.is_file():
                found.append(p)
    legacy = [home / "x64" / "modules" / "snegopat" / "addins" / "mcp" / "mcp_bridge.js",
              home / "x64" / "modules" / "snegopat" / "addins" / "mcp_bridge.js"]
    for p in legacy:
        if p.is_file() and p not in found:
            found.append(p)
    return found


def check_bridge_drift(home: Path) -> dict:
    """№431: дрейф моста — md5 каждого развёрнутого mcp_bridge.js против канона
    <репо>\\bridge\\mcp_bridge.js. Расхождение = warn (deployed-мост отстал от
    репозитория). Репозиторий не найден — note (doctor CLI работает и без репо);
    ни одной установки — note (о «не грузившемся» мосте отдельно говорит
    маркер загрузки, дублировать его не нужно)."""
    deployed = bridge_deploy_paths(home)
    if not deployed:
        return _check("дрейф моста", "note",
                      "mcp_bridge.js не найден ни в <дом>\\snegopat\\*, ни в "
                      "легаси <дом>\\x64\\modules\\snegopat\\addins\\",
                      "запустите install.ps1 (шаг «Синхронизация моста»); "
                      "грузился ли мост — см. маркер загрузки")
    if not REPO_BRIDGE.is_file():
        return _check("дрейф моста", "note",
                      f"репозиторий не найден ({REPO_BRIDGE}) — сверить не с чем")
    repo_md5 = _md5_prefix(REPO_BRIDGE)
    drift: list[str] = []
    same = 0
    for p in deployed:
        d = _md5_prefix(p)
        if d == repo_md5:
            same += 1
        else:
            drift.append(f"{p}: deploy {d} != repo {repo_md5}")
    if drift:
        return _check("дрейф моста", "warn",
                      "; ".join(drift) + f" (совпадают: {same})",
                      "запустите install.ps1 — актуальный мост переложится во все "
                      "установки; затем перезапустите конфигуратор")
    return _check("дрейф моста", "ok",
                  f"{len(deployed)} установк(и) моста совпадают с репозиторием "
                  f"(md5 {repo_md5}): " + "; ".join(str(p) for p in deployed))


def check_config_drift(cfg) -> dict:
    """№431: дрейф конфига — runtime-значения cfg против файла MCP_SNEGOPAT_CONFIG
    (если файл задан и существует). Отличие скаляров = warn «поля применены
    runtime'ом до файла». op_timeouts/long_ops вложенно не читаем — только
    факт наличия/отсутствия ключа. Нет env/файла — note."""
    env_path = os.environ.get("MCP_SNEGOPAT_CONFIG")
    if not env_path:
        return _check("дрейф конфига", "note",
                      "env MCP_SNEGOPAT_CONFIG не задан — сверять не с чем")
    f = Path(env_path)
    if not f.is_file():
        return _check("дрейф конфига", "note", f"файл конфига не найден: {f}")
    import json
    try:
        data = json.loads(f.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("не JSON-объект")
    except Exception as e:
        return _check("дрейф конфига", "warn", f"файл конфига не читается: {e}",
                      "исправьте JSON в файле конфига")
    diffs: list[str] = []
    for name in DRIFT_SCALARS:
        if name in data and getattr(cfg, name, None) != data[name]:
            diffs.append(name)
    for name in DRIFT_PRESENCE:
        if (name in data) != bool(getattr(cfg, name, None)):
            diffs.append(name + " (наличие)")
    if diffs:
        return _check("дрейф конфига", "warn",
                      "поля применены runtime'ом до файла: " + ", ".join(diffs),
                      "перезапустите MCP-сервер или внесите те же значения "
                      "в файл конфига")
    return _check("дрейф конфига", "ok", f"runtime-конфиг совпадает с {f}")


def _runtime_cfg(home: Path):
    """Живой cfg запущенного MCP-сервера (для MCP-блока doctor). server.py не
    правится: его doctor() зовёт run_filesystem_checks из этого модуля, значит
    mcp_snegopat.server уже в sys.modules — Toolkit-инстансы находим через gc.
    Предпочитаем инстанс, чей cfg.root.parent == home; иначе первый найденный."""
    mod = sys.modules.get("mcp_snegopat.server")
    if mod is None or not hasattr(mod, "Toolkit"):
        return None
    try:
        import gc
        found = [o for o in gc.get_objects() if isinstance(o, mod.Toolkit)]
    except Exception:
        return None
    home_p = Path(home)
    for tk in found:
        cfg = getattr(tk, "cfg", None)
        if cfg is not None and Path(getattr(cfg, "root", "")).parent == home_p:
            return cfg
    return getattr(found[0], "cfg", None) if found else None


def run_filesystem_checks(home: Path, journal_enabled: bool = True,
                          with_starter: bool = True, cfg=None) -> dict:
    """Полный файловый чек-лист (без живой сессии). {verdict, home, checks}.
    with_starter=False — для тестов (наличие процесса не файловый факт).
    cfg — runtime-конфиг MCP-сервера (MCP-блок): включает «дрейф конфига»;
    без cfg сервер ищется через _runtime_cfg (CLI-контекст — проверки нет)."""
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
    checks.append(check_bridge_drift(home))
    runtime_cfg = cfg if cfg is not None else _runtime_cfg(home)
    if runtime_cfg is not None:
        checks.append(check_config_drift(runtime_cfg))
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
        mark = {"ok": "ok  ", "warn": "warn", "fail": "FAIL",
                "note": "note"}.get(c["status"], "??  ")
        print(f"  [{mark}] {c['name']}: {c['detail']}")
        if c["hint"]:
            print(f"         подсказка: {c['hint']}")
    return 0 if res["verdict"] != "fail" else 1


if __name__ == "__main__":
    sys.exit(main())
