"""Этап 7: сценарий «создать → настроить → удалить → сохранить → cf» на копии БСП демо.

python spike/st7_cycle.py [--accept-warnings] [--tag X]
Пишет spike/out/st7_run_<tag>.json.
"""
import json
import sys
import time

sys.path.insert(0, "spike")
import client  # noqa: E402

ACCEPT = "--accept-warnings" in sys.argv
TAG = sys.argv[sys.argv.index("--tag") + 1] if "--tag" in sys.argv else time.strftime("%H%M%S")
C = "Справочник._ДемоКонтрагенты"
SINGULAR = {"Реквизиты": "Реквизит", "Справочники": "Справочник"}
o = {"log": []}


def focus(path):
    for i in range(3):
        client.call("tree_activate", {"path": path}, timeout=60)
        av = (client.call("active_view", {}, timeout=30).get("result") or {}).get("title")
        o["log"].append(f"activate {path} try{i}: {av}")
        if av and av.startswith("Конфигурация"):
            return True
    return False


def add(owner, kind, sib, name, comment=None):
    focus((owner + "." if owner else "") + SINGULAR[kind] + "." + sib)
    r = client.call("md_add_child", {"ownerPath": owner, "kind": kind, "sibling": sib, "method": "key",
                                     "noActivate": True, "requireTree": True, "newName": name, "comment": comment},
                    timeout=180)
    res = r.get("result") or {}
    return {"ok": r["ok"], "err": r.get("error"), "before": res.get("before"), "after": res.get("after"),
            "id": (res.get("created") or {}).get("id"), "renamed": res.get("renamed"), "ms": r.get("ms")}


t0 = time.time()
o["open"] = (client.call("config_open", {}, timeout=300).get("result") or {}).get("currentIdentifier")
o["a1"] = add(C, "Реквизиты", "Партнер", "МсрСтрока", "mcp: строка 50")
o["a1_type"] = (client.call("prop_set_type", {"path": C + ".Реквизит.МсрСтрока", "expectId": o["a1"]["id"],
                                              "type": {"types": ["Строка"], "length": 50}}, timeout=60).get("result") or {}).get("after")
o["a2"] = add(C, "Реквизиты", "Партнер", "МсрСумма", "mcp: число 15,2")
o["a2_type"] = (client.call("prop_set_type", {"path": C + ".Реквизит.МсрСумма", "expectId": o["a2"]["id"],
                                              "type": {"types": ["Число"], "digits": 15, "fraction": 2}}, timeout=60).get("result") or {}).get("after")
o["cat"] = add("", "Справочники", "_ДемоКонтрагенты", "МсрТестовыйСправочник", "mcp: справочник этапа 7")
o["a3"] = add(C, "Реквизиты", "Партнер", "МсрНаУдаление")
if o["a3"]["id"]:
    focus(C + ".Реквизит.МсрНаУдаление")
    d = client.call("md_delete", {"path": C + ".Реквизит.МсрНаУдаление", "uuid": o["a3"]["id"],
                                  "noActivate": True, "requireTree": True}, timeout=180)
    o["del"] = {"ok": d["ok"], "err": d.get("error"), "stillPresent": (d.get("result") or {}).get("stillPresent")}
o["edit_s"] = round(time.time() - t0, 1)

a = client.call("cfg_archive_cf", {"file": f"demossl-st7-{TAG}-before-save.cf"}, timeout=900).get("result") or {}
o["archive"] = {"ok": a.get("ok"), "ms": a.get("ms"), "file": a.get("file")}
s = client.call("config_save", {"archive": a.get("file", ""), "acceptWarnings": ACCEPT}, timeout=900).get("result") or {}
o["save"] = {"ok": s.get("ok"), "ms": s.get("ms"), "boxes": s.get("msgBoxes"), "messages": s.get("messages")}
if s.get("ok"):
    c = client.call("cfg_archive_cf", {"file": f"demossl-st7-{TAG}-result.cf"}, timeout=900).get("result") or {}
    o["cf"] = {"ok": c.get("ok"), "ms": c.get("ms"), "size": c.get("size"), "file": c.get("file")}
o["total_s"] = round(time.time() - t0, 1)
json.dump(o, open(f"spike/out/st7_run_{TAG}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
sys.stdout.buffer.write(json.dumps(o, ensure_ascii=False, indent=1).encode("utf-8"))
