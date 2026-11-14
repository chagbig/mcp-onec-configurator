"""Прицельная проба создания справочника: полный ответ моста для диагностики."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_snegopat.config import Config
from mcp_snegopat.server import Toolkit

tk = Toolkit(Config())
sess = tk.list_sessions()
pid = next(s["pid"] for s in sess["sessions"] if s["alive"])
print("pid:", pid)

print("\n-- открытие основной конфигурации --")
print(json.dumps(tk.config_open(session=pid), ensure_ascii=False)[:300])
import time
time.sleep(1)

print("\n-- существующие справочники --")
print(json.dumps(tk.md_children(session=pid, kind="Справочники"), ensure_ascii=False)[:400])

print("\n-- фокус: tree_activate корня --")
print(json.dumps(tk.tree_activate(session=pid), ensure_ascii=False))
print(json.dumps(tk.bridge.call(pid, "active_view"), ensure_ascii=False))

print("\n-- md_add_child: полный ответ --")
res = tk.md_add_child(session=pid, owner_path="", kind="Справочники", name="МсрПроба")
print(json.dumps(res, ensure_ascii=False, indent=1))

print("\n-- справочники после --")
print(json.dumps(tk.md_children(session=pid, kind="Справочники"), ensure_ascii=False)[:600])
