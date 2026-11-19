"""Живой сквозной прогон MCP-сервера по мосту в открытом конфигураторе (этапы 3-4).

Предусловия: запущены starter.exe и конфигуратор базы empty_mcp с мостом mcp_bridge.js
(файл включения Y:\\Snegopat-mcp\\mcp\\mcp_bridge.on). Прогон:

    python server/tests/live_e2e.py [--keep]

Без --keep созданные объекты удаляются и конфигурация сохраняется (isModified=false).
Отчёт — out/live_e2e_<tag>.json.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT_PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_snegopat.bridge import BridgeError, fnv1a, norm_eol  # noqa: E402
from mcp_snegopat.config import Config  # noqa: E402
from mcp_snegopat.server import Toolkit  # noqa: E402

KEEP = "--keep" in sys.argv
TAG = time.strftime("%H%M%S")
OUT = ROOT_PROJ / "out"
OUT.mkdir(exist_ok=True)
log: dict = {"steps": [], "failures": []}


def step(name: str, fn, *, check=None):
    t0 = time.time()
    try:
        res = fn()
        if check is not None:
            ok, why = check(res)
            if not ok:
                raise AssertionError(f"проверка не прошла: {why}")
        log["steps"].append({"step": name, "ok": True, "ms": round((time.time() - t0) * 1000)})
        print(f"  ok {name} ({(time.time() - t0) * 1000:.0f} мс)")
        return res
    except Exception as e:  # noqa: BLE001
        log["steps"].append({"step": name, "ok": False, "ms": round((time.time() - t0) * 1000),
                             "error": f"{type(e).__name__}: {e}",
                             "where": getattr(e, "where", None)})
        log["failures"].append(name)
        print(f"FAIL {name}: {type(e).__name__}: {e}")
        return None


def main() -> int:
    cfg = Config()  # умолчания: root=Y:\Snegopat-mcp\mcp, запись разрешена, хранилище/БД — нет
    tk = Toolkit(cfg)
    pid = None

    # ---------- сессии ----------
    sess = step("list_sessions", lambda: tk.list_sessions(),
                check=lambda r: (any(s["alive"] for s in r["sessions"]), "нет живых сессий"))
    if sess:
        # явный pid из аргументов: на машине могут быть чужие конфигураторы с мостом
        # (стартёр внедряется во все 1cv8.exe — инвариант 8 плана)
        arg_pid = next((int(a) for a in sys.argv[1:] if a.isdigit()), None)
        alive = [s["pid"] for s in sess["sessions"] if s["alive"]]
        if arg_pid:
            pid = arg_pid if arg_pid in alive else None
        elif len(alive) == 1:
            pid = alive[0]
        else:
            pid = None
            log["failures"].append("выбор сессии")
            print(f"живых сессий несколько ({alive}) — укажите pid аргументом")
    else:
        pid = None
    if pid is None:
        print("живых сессий нет (или не выбрана) — прогон невозможен")
        json.dump(log, open(OUT / f"live_e2e_{TAG}.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        return 2
    log["pid"] = pid

    step("session_info", lambda: tk.session_info(session=pid))
    step("config_open", lambda: tk.config_open(session=pid))
    step("session_info_after_open", lambda: tk.session_info(session=pid),
         check=lambda r: (len(r["containers"]) >= 2, f"контейнеров {len(r['containers'])}"))

    # ---------- чтение ----------
    step("md_counts", lambda: tk.md_counts(session=pid))
    step("md_tree_root", lambda: tk.md_tree(session=pid, limit=5))
    step("md_walk", lambda: tk.md_walk(session=pid))

    # ---------- создание (этап 7) ----------
    CAT = "МсрЭ2Э"
    cat_path = f"Справочник.{CAT}"
    created: dict = {}
    step("md_add_child: справочник",
         lambda: tk.md_add_child(session=pid, owner_path="", kind="Справочники",
                                 name=CAT, comment="live e2e"),
         check=lambda r: (r and r.get("renamed") and not r.get("stray"),
                          f"renamed={r and r.get('renamed')} stray={r and r.get('stray')}"))
    obj = step("md_object: справочник", lambda: tk.md_object(session=pid, path=cat_path),
               check=lambda r: (r and r.get("cls") == "Справочник", "класс не Справочник"))
    if obj:
        created["cat"] = obj["id"]
        log["modules_of_catalog"] = obj.get("modules")

    # ---------- модуль ----------
    mod_prop = None
    if obj:
        for m in obj.get("modules") or []:
            if m.get("prop") in ("МодульМенеджера", "МодульОбъекта"):
                mod_prop = m["prop"]
                break
    if mod_prop:
        got = step("module_get", lambda: tk.module_get(session=pid, path=cat_path,
                                                       prop=mod_prop, with_text=True))
        new_text = "// живой тест MCP\nПроцедура Тест()\n\tВозврат;\nКонецПроцедуры\n"
        if got:
            step("module_set",
                 lambda: tk.module_set(session=pid, path=cat_path, prop=mod_prop,
                                       text=new_text, expected_hash=got["hash"]),
                 check=lambda r: (r["hash"] == fnv1a(norm_eol(new_text)), "хеш после записи"))
            step("module_get_after_set",
                 lambda: tk.module_get(session=pid, path=cat_path, prop=mod_prop,
                                       with_text=True),
                 check=lambda r: (r["text"] == new_text, "текст не совпал"))
            step("module_patch",
                 lambda: tk.module_patch(session=pid, path=cat_path, prop=mod_prop,
                                         edits=[{"line": 1, "from": "// живой тест MCP",
                                                 "to": "// живой тест MCP (patched)"}],
                                         expected_hash=fnv1a(norm_eol(new_text))),
                 check=lambda r: (r["applied"] == 1, "не применено"))
            step("module_set_wrong_hash_refused",
                 lambda: wrong_hash_probe(tk, pid, cat_path, mod_prop),
                 check=lambda r: (r["refused"] and r["hashIntact"], "текст изменён при отказе"))
    else:
        log["modules_of_catalog"] = "нет свойств-модулей — пропуск module_*"

    # ---------- свойства ----------
    if obj is not None:
        comment_before = obj.get("comment") or ""
        step("md_set_property: комментарий",
             lambda: tk.md_set_property(session=pid, path=cat_path, prop="Комментарий",
                                        value="live e2e: правка комментария",
                                        expected=comment_before or None, uuid=created.get("cat")),
             check=lambda r: (r.get("after") == "live e2e: правка комментария", "не записалось"))

    # ---------- реквизит ----------
    attr = "МсрКод"
    attr_added = step("md_add_child: реквизит",
                      lambda: tk.md_add_child(session=pid, owner_path=cat_path,
                                              kind="Реквизиты", name=attr, comment="код e2e"),
                      check=lambda r: (r and r.get("renamed"), "не создан"))
    if attr_added:
        created["attr"] = attr_added["created"]["id"]
        step("md_set_type: строка 50",
             lambda: tk.md_set_type(session=pid, path=f"{cat_path}.Реквизит.{attr}",
                                    types=["Строка"], length=50, expect_id=created["attr"]),
             check=lambda r: (r["after"].get("length") == 50 or '"S",50' in str(r["after"]),
                              f"тип {r['after']}"))
        step("md_children: реквизиты",
             lambda: tk.md_children(session=pid, path=cat_path, kind="Реквизиты"),
             check=lambda r: (r["count"] == 1, f"реквизитов {r['count']}"))
        step("md_find: Мср",
             lambda: tk.md_find(session=pid, mask="Мср"),
             check=lambda r: (any(m["path"] == cat_path for m in r["matches"]), "не найден"))

    # ---------- проверка и сохранение ----------
    step("syntax_check", lambda: tk.syntax_check(session=pid),
         check=lambda r: (isinstance(r.get("messages"), list), "нет messages"))
    if not KEEP:
        # чистим за собой: реквизит, справочник; затем сохранение
        if "attr" in created:
            step("md_delete: реквизит",
                 lambda: tk.md_delete(session=pid, path=f"{cat_path}.Реквизит.{attr}",
                                      uuid=created["attr"]))
        if "cat" in created:
            step("md_delete: справочник",
                 lambda: tk.md_delete(session=pid, path=cat_path, uuid=created["cat"]))
    save = step("config_save (автоархив)", lambda: tk.config_save(session=pid),
                check=lambda r: (r.get("isModifiedAfter") is False,
                                 f"isModifiedAfter={r.get('isModifiedAfter')}"))
    if save:
        log["archive"] = save.get("archive")

    # ---------- идемпотентность и негативные пробы моста ----------
    raw_probes(tk, pid)

    step("bridge_stats", lambda: tk.bridge_stats(session=pid))

    json.dump(log, open(OUT / f"live_e2e_{TAG}.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\nитог: провалено {len(log['failures'])} из {len(log['steps'])}"
          f" ({'ЕСТЬ ПРОВАЛЫ' if log['failures'] else 'ВСЕ ШАГИ ОК'})")
    print(f"отчёт: out/live_e2e_{TAG}.json")
    return 1 if log["failures"] else 0


def wrong_hash_probe(tk: Toolkit, pid: int, path: str, prop: str):
    """Негатив: неверный expected_hash — отказ, текст не тронут (инварианты 3-4)."""
    got = tk.module_get(session=pid, path=path, prop=prop)
    try:
        tk.module_set(session=pid, path=path, prop=prop, text="затёрто?",
                      expected_hash="00000000")
    except BridgeError as e:
        assert "expected_hash" in str(e), f"не тот отказ: {e}"
        after = tk.module_get(session=pid, path=path, prop=prop)
        assert after["hash"] == got["hash"], "текст изменился при отказе!"
        return {"refused": True, "hashIntact": True, "code": e.code}
    raise AssertionError("отказ не возник")


def raw_probes(tk: Toolkit, pid: int) -> None:
    """Сырые пробы протокола: идемпотентность, 6 битых запросов, deadline, unknown op."""
    import os
    import uuid as uuid_mod
    sess_dir = Path(tk.cfg.root) / "sessions" / str(pid)
    req = sess_dir / "req"
    resp = sess_dir / "resp"

    def raw(rid: str, body: dict | str, wait_s: float = 10.0) -> dict | None:
        tmp = req / f"{rid}.tmp"
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, req / f"{rid}.json")
        t0 = time.time()
        while time.time() - t0 < wait_s:
            p = resp / f"{rid}.json"
            if p.is_file():
                try:
                    data = json.loads(p.read_text(encoding="utf-8-sig"))
                    p.unlink(missing_ok=True)
                    return data
                except (json.JSONDecodeError, OSError):
                    pass
            time.sleep(0.03)
        return None

    stats = lambda: tk.bridge_stats(session=pid)  # noqa: E731

    # идемпотентность: тот же id дважды — прежний ответ, replayed растёт;
    # done может вырасти не больше чем на 1 (сам вызов статистики)
    rid = "manual-" + uuid_mod.uuid4().hex[:8]
    r1 = raw(rid, {"op": "ping", "args": {}})
    st1 = stats()
    r2 = raw(rid, {"op": "ping", "args": {}})
    st2 = stats()
    ok = (r1 and r2 and r1.get("result", {}).get("pong") and r2.get("result", {}).get("pong")
          and r1.get("result") == r2.get("result")
          and st2["replayed"] == st1["replayed"] + 1
          and 0 <= st2["done"] - st1["done"] <= 1)
    log["steps"].append({"step": "raw: идемпотентность по id", "ok": bool(ok),
                         "replayed": (st1.get("replayed"), st2.get("replayed")),
                         "done": (st1.get("done"), st2.get("done"))})
    print(f"  {'ok ' if ok else 'FAIL'} raw: идемпотентность по id")

    # 6 битых запросов подряд — мост жив (критерий этапа 2)
    before = stats()
    for i in range(6):
        raw(f"bad-{uuid_mod.uuid4().hex[:8]}", "{это не json]}", wait_s=3.0)
    pong = tk.bridge.call(pid, "ping", timeout=15)
    alive = pong.get("pong") is True
    log["steps"].append({"step": "raw: 6 битых запросов -> мост жив", "ok": alive})
    print(f"  {'ok ' if alive else 'FAIL'} raw: 6 битых запросов -> мост жив")

    # истёкший deadline — не выполняется
    r = raw(f"dl-{uuid_mod.uuid4().hex[:8]}", {"op": "session_info", "deadline": 1000})
    ok = r and not r.get("ok") and r.get("error", {}).get("code") == "deadline"
    log["steps"].append({"step": "raw: deadline", "ok": bool(ok)})
    print(f"  {'ok ' if ok else 'FAIL'} raw: deadline -> {r and r.get('error', {}).get('code')}")

    # неизвестная операция
    r = raw(f"unk-{uuid_mod.uuid4().hex[:8]}", {"op": "нет_такой"})
    ok = r and not r.get("ok") and r.get("error", {}).get("code") == "unknown_op"
    log["steps"].append({"step": "raw: unknown_op", "ok": bool(ok)})
    print(f"  {'ok ' if ok else 'FAIL'} raw: unknown_op")


if __name__ == "__main__":
    sys.exit(main())
