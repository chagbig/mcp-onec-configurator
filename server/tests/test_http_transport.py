# -*- coding: utf-8 -*-
"""№13/№27 (v1.7.0): MCP over streamable-http — auth-мидлвэр, CLI-ключи,
гвард нелокального слушателя и e2e-смоук поднимающегося сервера (skipUnless
uvicorn: CI без него не падает)."""
from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from mcp_snegopat.config import Config
from mcp_snegopat.server import _auth_asgi, _is_local_host, _parse_http_args



def _err_texts(exc: BaseException) -> str:
    """Все сообщения из дерева ExceptionGroup одной строкой."""
    parts = [str(exc)]
    for sub in getattr(exc, "exceptions", ()):
        parts.append(_err_texts(sub))
    return " ".join(parts)

class _Recorder:
    """Простейший downstream-приёмник ASGI-вызовов."""

    def __init__(self):
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.scope = scope
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})


async def _call(app, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)
    return sent


class TestAuthAsgi(unittest.TestCase):
    def test_401_without_token(self) -> None:
        rec = _Recorder()
        app = _auth_asgi(rec, "secret")
        sent = _run(_call(app, {"type": "http", "headers": []}))
        self.assertEqual(sent[0]["status"], 401)
        self.assertIsNone(rec.scope)  # downstream не вызван

    def test_401_wrong_token(self) -> None:
        rec = _Recorder()
        app = _auth_asgi(rec, "secret")
        hdr = [(b"authorization", b"Bearer other")]
        sent = _run(_call(app, {"type": "http", "headers": hdr}))
        self.assertEqual(sent[0]["status"], 401)

    def test_pass_with_token(self) -> None:
        rec = _Recorder()
        app = _auth_asgi(rec, "secret")
        hdr = [(b"authorization", b"Bearer secret")]
        sent = _run(_call(app, {"type": "http", "headers": hdr}))
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(rec.scope["type"], "http")

    def test_non_http_scope_passthrough(self) -> None:
        rec = _Recorder()
        app = _auth_asgi(rec, "secret")
        sent = _run(_call(app, {"type": "lifespan"}))
        self.assertEqual(sent[0]["status"], 200)  # без авторизации — passthrough


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class TestLocalHost(unittest.TestCase):
    def test_local_variants(self) -> None:
        for h in ("127.0.0.1", "localhost", "::1", "LOCALHOST"):
            self.assertTrue(_is_local_host(h))
        for h in ("0.0.0.0", "", "192.168.1.5", "station.lan"):
            self.assertFalse(_is_local_host(h))


class TestParseArgs(unittest.TestCase):
    def test_no_http(self) -> None:
        self.assertEqual(_parse_http_args([]), {})

    def test_http_bare(self) -> None:
        self.assertEqual(_parse_http_args(["--http"]), {"http_enabled": True})

    def test_http_host_port(self) -> None:
        res = _parse_http_args(["--http", "0.0.0.0:8944"])
        self.assertEqual(res, {"http_enabled": True, "http_host": "0.0.0.0",
                               "http_port": 8944})

    def test_auth_token(self) -> None:
        res = _parse_http_args(["--http", "--auth-token", "abc"])
        self.assertEqual(res["auth_token"], "abc")


class TestGuard(unittest.TestCase):
    def test_refuse_remote_without_token(self) -> None:
        cfg = Config(root=Path(tempfile.mkdtemp()), http_enabled=True,
                     http_host="0.0.0.0", auth_token="")
        with self.assertRaises(SystemExit) as cm:
            from mcp_snegopat.server import run_http
            run_http(cfg)
        self.assertIn("auth_token", str(cm.exception))

    def test_local_without_token_allowed(self) -> None:
        # гвард не срабатывает на 127.0.0.1 (сам run_http не зовём —
        # проверяем только условие, чтобы тест не поднимал uvicorn)
        from mcp_snegopat.server import _is_local_host
        self.assertTrue(_is_local_host("127.0.0.1"))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(
    importlib.util.find_spec("uvicorn") is not None,
    "uvicorn недоступен — e2e-смоук HTTP-транспорта пропущен")
class TestHttpE2E(unittest.TestCase):
    """Живой смоук: подпроцесс-сервер --http + клиент SDK (init + list_tools)
    с токеном; без токена — отказ соединения (401)."""

    SRV = 20  # секунд на подъём сервера

    def _start(self, port: int, token: str) -> subprocess.Popen:
        cmd = [sys.executable, "-m", "mcp_snegopat", "--http",
               "127.0.0.1:%d" % port, "--auth-token", token]
        env = dict(os.environ)
        env.setdefault("MCP_SNEGOPAT_CONFIG", "")
        return subprocess.Popen(cmd, cwd=str(Path(__file__).resolve().parents[1]),
                                env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    def test_init_and_list_tools_with_token(self) -> None:
        import asyncio
        from mcp.client.streamable_http import (create_mcp_http_client,
                                                streamable_http_client)
        from mcp import ClientSession

        port = _free_port()
        token = "e2e-token"
        proc = self._start(port, token)
        try:
            async def flow():
                http = create_mcp_http_client(
                    headers={"Authorization": "Bearer " + token})
                async with streamable_http_client(
                        "http://127.0.0.1:%d/mcp" % port,
                        http_client=http) as (r, w):
                    async with ClientSession(r, w) as sess:
                        await sess.initialize()
                        tools = await sess.list_tools()
                        names = [t.name for t in tools.tools]
                return names

            names = None
            import time
            deadline = time.time() + self.SRV
            while time.time() < deadline:
                try:
                    names = asyncio.run(asyncio.wait_for(flow(), timeout=15))
                    break
                except (ConnectionError, OSError, BaseException) as e:
                    if proc.poll() is not None:
                        self.fail("сервер-подпроцесс упал при подъёме")
                    if "401" in str(e) or "unauthorized" in str(e).lower():
                        raise
                    time.sleep(0.5)
            self.assertIsNotNone(names, "сервер не поднялся за %d с" % self.SRV)
            self.assertIn("list_sessions", names)
            self.assertIn("journal_dashboard", names)
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_wrong_token_rejected(self) -> None:
        import asyncio
        from mcp.client.streamable_http import (create_mcp_http_client,
                                                streamable_http_client)
        from mcp import ClientSession
        port = _free_port()
        proc = self._start(port, "right-token")
        try:
            async def flow():
                http = create_mcp_http_client(
                    headers={"Authorization": "Bearer wrong"})
                async with streamable_http_client(
                        "http://127.0.0.1:%d/mcp" % port,
                        http_client=http) as (r, w):
                    async with ClientSession(r, w) as sess:
                        await sess.initialize()

            rejected = False
            import time
            deadline = time.time() + self.SRV
            while time.time() < deadline:
                try:
                    asyncio.run(asyncio.wait_for(flow(), timeout=15))
                    break
                except BaseException as e:
                    if "unauthorized" in _err_texts(e).lower() or "401" in _err_texts(e):
                        rejected = True
                        break
                    if proc.poll() is not None:
                        self.fail("сервер-подпроцесс упал")
                    time.sleep(0.5)
            self.assertTrue(rejected, "ожидался отказ 401 при неверном токене")
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
