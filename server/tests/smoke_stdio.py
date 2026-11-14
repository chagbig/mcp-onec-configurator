"""Смоук-тест MCP-сервера по stdio: initialize -> tools/list -> tools/call.

Запуск: python server/tests/smoke_stdio.py [каталог_моста]
Без аргумента — временный пустой каталог (list_sessions должен вернуть пусто без ошибки).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def rpc(proc: subprocess.Popen, obj: dict) -> None:
    proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def read(proc: subprocess.Popen, want_id: int, timeout_s: float = 30.0) -> dict:
    import threading
    result: dict = {}
    err = []

    def reader():
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want_id:
                result["msg"] = msg
                return
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout_s)
    if not result:
        raise TimeoutError(f"нет ответа на id={want_id}")
    return result["msg"]


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="mcp-smoke-")
    cfg_path = Path(root) / "smoke-config.json"
    cfg_path.write_text(json.dumps({"root": root}), encoding="utf-8")
    env = dict(os.environ, MCP_SNEGOPAT_CONFIG=str(cfg_path),
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    server_dir = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_snegopat"],
        cwd=server_dir, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8")
    try:
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"}}})
        init = read(proc, 1)
        info = init["result"]["serverInfo"]
        print("initialize:", info["name"], info.get("version"))
        rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = read(proc, 2)["result"]["tools"]
        print(f"tools/list: {len(tools)} инструментов")
        rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "list_sessions", "arguments": {}}})
        call = read(proc, 3)
        assert not call["result"].get("isError"), call
        text = call["result"]["content"][0]["text"]
        payload = json.loads(text)
        print("list_sessions ->", json.dumps(payload, ensure_ascii=False)[:120])
        assert "sessions" in payload
        print("SMOKE OK")
        return 0
    finally:
        proc.kill()


if __name__ == "__main__":
    sys.exit(main())
