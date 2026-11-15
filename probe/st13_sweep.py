# -*- coding: utf-8 -*-
"""Перебор команд Config 38-46 с поиском окна «Расширения конфигурации».
Разрешено владельцем 28.10.2026 (слепой перебор = риск модалок, восстановление — taskkill)."""
import sys, time, os, json, glob, subprocess
sys.path.insert(0, r"Y:\mcp-configurator\server")
from mcp_snegopat.config import load_config
from mcp_snegopat.bridge import Bridge

BASE = r"Y:\Snegopat-mcp\bases\empty2"

def clean_locks():
    for pat in ("*.cfl", "1Cv8tmp.1CD*", "1Cv8snc.1CD*"):
        for f in glob.glob(BASE + "\\" + pat):
            try:
                os.remove(f)
            except OSError:
                pass

def relaunch():
    subprocess.Popen([r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe", "DESIGNER", "/F", BASE])
    time.sleep(38)
    br = Bridge(load_config())
    pid = next((s.pid for s in br.sessions() if s.alive and "empty2" in str(s.data.get("conn") or "")), None)
    if pid:
        br.call(pid, "config_open", {}, timeout=180)
    return pid

def clean_and_relaunch(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    time.sleep(3)
    clean_locks()
    return relaunch()

def state(br, pid):
    try:
        si = br.call(pid, "session_info", {}, timeout=15)
    except Exception:
        return None, None, None
    v = br.call(pid, "view_list", {}, timeout=20)
    titles = [x.get("title") for x in (v if isinstance(v, list) else v.get("views", [])) if x.get("title")]
    mf = r"Y:\Snegopat-mcp\mcp\sessions\%d\modals.jsonl" % pid
    last_modal = None
    if os.path.isfile(mf):
        lines = open(mf, encoding="utf-8-sig").readlines()
        if lines:
            last_modal = json.loads(lines[-1]).get("caption")
    return si.get("modalMode"), titles, last_modal

def main():
    br = Bridge(load_config())
    pid = next((s.pid for s in br.sessions() if s.alive and "empty2" in str(s.data.get("conn") or "")), None)
    assert pid, "нет живой сессии empty2"
    if not os.path.isfile(r"Y:\Snegopat-mcp\mcp\mcp_dump_modals.on"):
        open(r"Y:\Snegopat-mcp\mcp\mcp_dump_modals.on", "w").write("on")
    br.call(pid, "view_activate", {"title": "Конфигурация"}, timeout=30)
    results = {}
    found = None
    for num in [38, 41, 43, 45, 46]:
        print("=== команда", num, flush=True)
        try:
            br.call(pid, "send_cfg_cmd", {"num": num}, timeout=30)
        except Exception as e:
            print("  отказ:", str(e)[:80])
            results[num] = "refused"
            continue
        time.sleep(2.5)
        mm, titles, modal = state(br, pid)
        if mm is None:
            print("  мост умер — восстановление")
            results[num] = "hang-recovered"
            pid = clean_and_relaunch(pid)
            continue
        ext = [t for t in (titles or []) if t and "Расширени" in t]
        print("  modalMode:", mm, "| модал:", modal, "| окна с Расширени:", ext)
        if mm and mm > 0:
            results[num] = "modal:%s" % modal
            pid = clean_and_relaunch(pid)
            continue
        if modal and "Каталоги" in str(modal):
            results[num] = "catalog-dialog"
            ps = ("(New-Object -ComObject WScript.Shell).AppActivate('Каталоги шаблонов конфигураций')|Out-Null;"
                  "Start-Sleep -m 300;(New-Object -ComObject WScript.Shell).SendKeys('{ESC}')")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
            time.sleep(1.5)
            continue
        if ext:
            print("  *** МЕНЕДЖЕР РАСШИРЕНИЙ НАЙДЕН (команда", num, ") ***")
            results[num] = "EXT-MANAGER"
            found = num
            break
        results[num] = "no-window"
    print("итог:", results)
    print("найдено: команда", found)
    print("живой pid:", pid)
    return 0

if __name__ == "__main__":
    sys.exit(main())
