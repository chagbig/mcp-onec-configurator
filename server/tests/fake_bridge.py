"""Подставной мост для тестов: поток, обслуживающий почтовый каталог как mcp_bridge.js.

Реализует подмножество операций с той же семантикой ответов:
{op, args, deadline} -> {id, ok, result | error, ms}. Идемпотентность по id —
как в настоящем мосте (повтор id отдаёт прежний ответ).
"""
from __future__ import annotations

import json
import os
import threading
import time

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_snegopat.bridge import fnv1a, norm_eol  # noqa: E402


class FakeBridge:
    def __init__(self, root: Path, pid: int):
        self.root = Path(root)
        self.sess = self.root / "sessions" / str(pid)
        (self.sess / "req").mkdir(parents=True, exist_ok=True)
        (self.sess / "resp").mkdir(parents=True, exist_ok=True)
        self.pid = pid
        self.modules: dict[tuple[str, str], str] = {}  # (path, prop) -> text
        self.objects: dict[str, dict] = {}              # path -> md_object-подобный ответ
        self.children: dict[tuple[str, str], list] = {}  # (path, kind) -> [{name, id}]
        self.kind_items: dict[str, list] = {}           # kind -> [{name, cls, id}] (md_tree)
        self.type_sets: list[dict] = []                 # вызовы prop_set_type(_internal)
        self.active_title = "Конфигурация — Демо"
        self.focus_fail_times = 0
        self.platform = "8.3.27.2074"
        self.args_seen: list[tuple[str, dict]] = []
        self._cache: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    # ---------- жизненный цикл ----------

    def start(self) -> "FakeBridge":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def heartbeat(self) -> None:
        self._write_session()

    def _write_session(self) -> None:
        data = {"pid": self.pid, "conn": 'File="C:\\fake\\base";', "platform": self.platform,
                "heartbeat": int(time.time() * 1000), "containers": [
                    {"index": 0, "identifier": "main", "isModified": False, "root": "Конфигурация"}]}
        self._atomic_write(self.sess / "session.json", json.dumps(data))

    # ---------- цикл обработки ----------

    def _loop(self) -> None:
        self._write_session()
        req_dir = self.sess / "req"
        while not self._stop.is_set():
            try:
                files = sorted(p for p in req_dir.glob("*.json"))
            except OSError:
                time.sleep(0.01)
                continue
            for req_path in files:
                self._handle(req_path)
            time.sleep(0.01)

    def _handle(self, req_path: Path) -> None:
        rid = req_path.name[:-5]
        resp_path = self.sess / "resp" / f"{rid}.json"
        if rid in self._cache:  # идемпотентность, как в мосте
            resp_path.write_text(self._cache[rid], encoding="utf-8")
            req_path.unlink(missing_ok=True)
            return
        t0 = time.time()
        resp: dict = {"id": rid, "ok": False}
        try:
            req = json.loads(req_path.read_text(encoding="utf-8-sig"))
            req_path.unlink(missing_ok=True)
            op = req.get("op")
            self.args_seen.append((op, req.get("args") or {}))
            if req.get("deadline") and time.time() * 1000 > req["deadline"]:
                resp["error"] = {"code": "deadline", "message": "истёк срок запроса"}
            elif not isinstance(op, str) or not hasattr(self, f"op_{op}"):
                resp["error"] = {"code": "unknown_op", "message": f"неизвестная операция {op}"}
            else:
                resp["result"] = getattr(self, f"op_{op}")(req.get("args") or {})
                resp["ok"] = True
        except Exception as e:  # noqa: BLE001 — как мост: любая ошибка в error
            resp["error"] = {"code": "exception", "message": str(e)}
        resp["ms"] = int((time.time() - t0) * 1000)
        text = json.dumps(resp, ensure_ascii=False)
        self._atomic_write(resp_path, text)
        self._cache[rid] = text
        if len(self._cache) > 256:
            self._cache.pop(next(iter(self._cache)))

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    # ---------- операции ----------

    def op_ping(self, a: dict) -> dict:
        return {"pong": True, "bridge": "fake"}

    def op_session_info(self, a: dict) -> dict:
        return {"pid": self.pid, "containers": [{"index": 0, "identifier": "main"}]}

    def op_timer_stats(self, a: dict) -> dict:
        return {"ticks": 1, "done": len(self._cache)}

    def op_wait(self, a: dict) -> dict:
        return {"id": a.get("id"), "state": "done"}

    def op_active_view(self, a: dict) -> dict:
        if self.focus_fail_times > 0:
            self.focus_fail_times -= 1
            return {"title": "Свойства"}
        return {"title": self.active_title}

    def op_focus_probe(self, a: dict) -> dict:
        return {"focusHwnd": 12345 if not self.focus_fail_times else 0,
                "modalMode": 0, "activeTitle": self.active_title}

    def op_activate_self(self, a: dict) -> dict:
        return {"hadFocus": not self.focus_fail_times, "appActivate": True, "focus": 12345}

    def op_tree_activate(self, a: dict) -> dict:
        return {"name": a.get("path") or "Конфигурация", "id": "root"}

    def op_md_object(self, a: dict) -> dict:
        path = a.get("path") or ""
        if path in self.objects:
            return self.objects[path]
        if path == "":
            return {"name": "Конфигурация", "id": "root", "cls": "Конфигурация"}
        raise RuntimeError(f"не найден {path}")

    def op_md_tree(self, a: dict) -> dict:
        kind = (a.get("kinds") or [""])[0] if a.get("kinds") else ""
        items = self.kind_items.get(kind, [])
        return {"obj": {"name": "Конфигурация"}, "kinds": [{"kind": "Справочники", "count": len(items)}],
                "total": len(items), "items": items, "hasMore": False}

    def op_md_counts(self, a: dict) -> dict:
        kinds = [{"kind": k, "count": len(items)}
                 for k, items in self.kind_items.items()] or \
                [{"kind": "Справочники", "count": 0}]
        return {"kinds": kinds}

    def op_md_find(self, a: dict) -> dict:
        """Поиск по подстроке (без учёта регистра) по зарегистрированным объектам:
        имя объекта И синоним (spec_check №130 фиксирует семантику «similar»);
        формат совпадений — как в мосте: {path, name, id, cls}."""
        mask = str(a.get("mask") or "").lower()
        if not mask:
            raise RuntimeError("пустая маска поиска")
        want_kind = str(a.get("kind") or "").lower() or None
        limit = a.get("limit") or 50
        matches = []
        for path in sorted(self.objects):
            obj = self.objects[path] or {}
            cls = str(obj.get("cls") or path.split(".")[0])
            if want_kind and cls.lower() != want_kind:
                continue
            name = str(obj.get("name") or path.split(".")[-1])
            if mask in name.lower() or mask in str(obj.get("synonym") or "").lower():
                matches.append({"path": path, "name": name,
                                "id": str(obj.get("id") or ""), "cls": cls})
        truncated = len(matches) > limit
        return {"mask": a.get("mask"), "kind": a.get("kind"),
                "matches": matches[:limit], "truncated": truncated}

    def op_md_children(self, a: dict) -> dict:
        items = self.children.get((a.get("path") or "", a.get("kind") or ""), [])
        return {"kind": a.get("kind"), "count": len(items), "items": items}

    def op_prop_set_type(self, a: dict) -> dict:
        self.type_sets.append({"path": a.get("path"), "type": a.get("type"),
                               "internal": None})
        return {"before": None, "set": True, "after": a.get("type")}

    def op_prop_set_type_internal(self, a: dict) -> dict:
        self.type_sets.append({"path": a.get("path"), "type": None,
                               "internal": a.get("internal")})
        return {"before": None, "set": True, "after": a.get("internal")}

    def op_module_get(self, a: dict) -> dict:
        key = (a.get("path") or "", a.get("prop") or "Модуль")
        if key not in self.modules:
            raise RuntimeError(f"свойство не модуль: {key[1]}")
        text = self.modules[key]
        norm = norm_eol(text)
        out = {"hash": fnv1a(norm), "lines": len(norm.split("\n")) if norm else 0,
               "length": len(text), "readOnly": False}
        if a.get("withText"):
            out["text"] = text
        return out

    def op_module_set(self, a: dict) -> dict:
        key = (a.get("path") or "", a.get("prop") or "Модуль")
        text = self.modules.get(key)
        if text is None:
            raise RuntimeError("свойство не модуль")
        if a.get("expected_hash") != fnv1a(norm_eol(text)):
            raise RuntimeError(f"expected_hash не совпал: текущий {fnv1a(norm_eol(text))}")
        self.modules[key] = a.get("text") or ""
        return {"hash": fnv1a(norm_eol(self.modules[key])), "applied": True}

    def op_module_patch(self, a: dict) -> dict:
        key = (a.get("path") or "", a.get("prop") or "Модуль")
        text = self.modules.get(key)
        if text is None:
            raise RuntimeError("свойство не модуль")
        norm = norm_eol(text)
        if a.get("expected_hash") != fnv1a(norm):
            raise RuntimeError("expected_hash не совпал")
        trailing = norm.endswith("\n")
        lines = norm.split("\n")
        if trailing:
            lines.pop()
        for e in a.get("edits") or []:
            li = e["line"] - 1
            if not (0 <= li < len(lines)):
                raise RuntimeError(f"строка вне модуля: {e['line']}")
            if lines[li] != e["from"]:
                raise RuntimeError(f"строка {e['line']} не совпала: «{lines[li]}»")
            lines[li] = e["to"]
        self.modules[key] = "\n".join(lines) + ("\n" if trailing else "")
        return {"applied": len(a.get("edits") or []), "hash": fnv1a(norm_eol(self.modules[key]))}

    _add_counter = 0

    def op_md_add_child(self, a: dict) -> dict:
        self._add_counter += 1
        expect = a.get("expectClass") or "Справочник"
        return {"sent": True, "created": {"id": f"uuid-new-{self._add_counter}",
                                          "name": a.get("newName"), "cls": expect},
                "renamed": True, "stray": [], "navKeys": a.get("navKeys"),
                "ownerPath": a.get("ownerPath"), "kind": a.get("kind")}

    def op_md_delete(self, a: dict) -> dict:
        return {"id": a.get("uuid"), "before": 1, "after": 0, "boxes": [], "modals": []}

    def op_prop_set(self, a: dict) -> dict:
        return {"setResult": True, "before": a.get("expected"), "after": a.get("value")}

    def op_prop_set_internal(self, a: dict) -> dict:
        return {"before": '{"U"}', "set": True, "after": a.get("internal")}

    def op_update_db_cfg(self, a: dict) -> dict:
        if not a.get("own_bases"):
            raise RuntimeError("список own_bases пуст")
        return {"sent": True, "own_bases": a["own_bases"]}

    def op_cfgstore_capture(self, a: dict) -> dict:
        return {"sent": True}

    def op_cfgstore_commit(self, a: dict) -> dict:
        if not (a.get("comment") or "").strip():
            raise RuntimeError("комментарий обязателен")
        return {"sent": True}

    def op_slow_op(self, a: dict) -> dict:
        time.sleep(1.2)
        return {"slept": 1.2}

    def op_error_op(self, a: dict) -> dict:
        raise RuntimeError("плановая ошибка")
