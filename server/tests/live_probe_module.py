"""Прицельная проба цикла модуля после активации окна (разбор зависания 23.10).

Шаги: focus_probe -> activate_window -> md_add_child (путь дерева) ->
module_get -> module_set -> module_patch. Любой шаг печатается сразу (flush).
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_snegopat import winutils
from mcp_snegopat.bridge import fnv1a, norm_eol
from mcp_snegopat.config import Config
from mcp_snegopat.server import Toolkit


def out(name, obj=None):
    print(name, json.dumps(obj, ensure_ascii=False, default=str)[:300] if obj is not None else "",
          flush=True)


tk = Toolkit(Config())
pid = tk.bridge.pick_session()
out("pid:", pid)

out("config_open:", tk.config_open(session=pid))
time.sleep(0.5)
out("focus до:", tk.focus_probe(session=pid))
out("activate:", winutils.activate_window(pid))
time.sleep(0.3)
out("focus после:", tk.focus_probe(session=pid))

CAT = "МсрМодуль"
res = tk.md_add_child(session=pid, owner_path="", kind="Справочники", name=CAT,
                      comment="проба модуля")
out("add_child:", {k: res.get(k) for k in ("renamed", "stray")})
cat_id = (res.get("created") or {}).get("id")
out("cat uuid:", cat_id)

obj = tk.md_object(session=pid, path=f"Справочник.{CAT}")
mods = [m.get("prop") for m in obj.get("modules") or []]
out("модули объекта:", mods)
prop = "МодульМенеджера" if "МодульМенеджера" in mods else (mods[0] if mods else None)
out("выбран модуль:", prop)
if not prop:
    sys.exit(0)

got = tk.module_get(session=pid, path=f"Справочник.{CAT}", prop=prop, with_text=True)
out("module_get:", {k: got.get(k) for k in ("hash", "lines")})

text = "// проба\nПроцедура П()\n\tВозврат;\nКонецПроцедуры\n"
res = tk.module_set(session=pid, path=f"Справочник.{CAT}", prop=prop,
                    text=text, expected_hash=got["hash"])
out("module_set:", {k: res.get(k) for k in ("hash", "linesAfter")})

res = tk.module_patch(session=pid, path=f"Справочник.{CAT}", prop=prop,
                      edits=[{"line": 1, "from": "// проба", "to": "// проба (patched)"}],
                      expected_hash=fnv1a(norm_eol(text)))
out("module_patch:", {k: res.get(k) for k in ("applied", "hash")})

after = tk.module_get(session=pid, path=f"Справочник.{CAT}", prop=prop, with_text=True)
out("текст после:", after.get("text"))
out("ВСЁ ОК" if after.get("text", "").startswith("// проба (patched)") else "РАСХОЖДЕНИЕ")
