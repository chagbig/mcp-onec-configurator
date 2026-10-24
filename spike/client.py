"""Клиент опытов этапа 1: кладёт запрос в почтовый каталог моста и ждёт ответа.

Пример:
    python spike/client.py ping
    python spike/client.py md_counts
    python spike/client.py module_texts "{\"path\": \"ОбщийМодуль.ОбщегоНазначения\", \"prop\": \"Модуль\"}"
"""
import json
import os
import sys
import time
import uuid

ROOT = r"Y:\Snegopat-mcp\mcp"


def norm_eol(t: str) -> str:
    return t.replace("\r\n", "\n").replace("\r", "\n")


def fnv1a(t: str) -> str:
    """FNV-1a 32 по кодовым единицам UTF-16 — так же, как fnv1a() в mcp_spike.js."""
    h = 0x811C9DC5
    data = t.encode("utf-16-le")
    for i in range(0, len(data), 2):
        h ^= data[i] | (data[i + 1] << 8)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def call(op: str, args: dict | None = None, timeout: float = 60.0) -> dict:
    rid = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    req = {"op": op, "args": args or {}, "deadline": int((time.time() + timeout) * 1000)}
    tmp = os.path.join(ROOT, "req", rid + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(req, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(ROOT, "req", rid + ".json"))
    resp_path = os.path.join(ROOT, "resp", rid + ".json")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(resp_path):
            for _ in range(20):
                try:
                    with open(resp_path, encoding="utf-8-sig") as f:
                        resp = json.load(f)
                    os.remove(resp_path)
                    resp["_wall_ms"] = int((time.time() - t0) * 1000)
                    return resp
                except (PermissionError, json.JSONDecodeError):
                    time.sleep(0.05)
        time.sleep(0.02)
    return {"id": rid, "ok": False, "error": {"code": "timeout", "message": f"нет ответа за {timeout} с"}}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    op = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(call(op, args), ensure_ascii=False, indent=1))
