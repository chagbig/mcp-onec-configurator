"""Клиент почтового каталога моста (транспорт A) и реестр сессий.

Мост (аддин mcp_bridge.js в конфигураторе) опрашивает
<root>/sessions/<pid>/req раз в 150 мс и кладёт ответ в
<root>/sessions/<pid>/resp/<id>.json. Реестр сессии — session.json
рядом, обновляется мостом (heartbeat, контейнеры, running).
"""
from __future__ import annotations

import ctypes
import json
import os
import time
import uuid
from pathlib import Path

from .config import Config

# Долгие операции моста: статус running в сессии, таймаут клиента не «не выполнено»
LONG_OPS = {
    "config_open", "cfg_archive_cf", "config_save", "syntax_check",
    "update_db_cfg", "cfgstore_capture", "cfgstore_commit",
}

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class BridgeError(RuntimeError):
    """Ошибка от моста или транспорта. code: unknown_op | deadline | exception | timeout | ..."""

    def __init__(self, code: str, message: str, where: dict | None = None):
        super().__init__(message)
        self.code = code
        self.where = where or {}


class NoSessionError(BridgeError):
    def __init__(self, message: str):
        super().__init__("no_session", message)


def pid_alive(pid: int) -> bool:
    """Жив ли процесс (Windows). Мёртвый pid с файлом сессии — сессия мёртвая."""
    if pid <= 0:
        return False
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def platform_supported(platform) -> bool:
    """Проверенная ветка платформы — 8.3.27 (сборка Снегопата снята по её vtable;
    на 8.3.28+ слоты сдвигаются — методы могут отдавать мусор, план §6)."""
    return str(platform or "").startswith("8.3.27")


def norm_file_key(path) -> str:
    """Нормализация файлового пути для сравнения строк соединения — на манер
    normFilePath моста: / -> \\, нижний регистр, без хвостовых \\ (ревью 25.10.2026:
    mcp_to_line сравнивал по своей схеме и расходился с мостом)."""
    s = str(path).replace("/", "\\").strip().lower()
    while len(s) > 3 and s.endswith("\\"):
        s = s[:-1]
    return s


def norm_eol(t: str) -> str:
    return t.replace("\r\n", "\n").replace("\r", "\n")


def fnv1a(t: str) -> str:
    """FNV-1a 32 по кодовым единицам UTF-16 — так же, как fnv1a() в mcp_bridge.js."""
    h = 0x811C9DC5
    data = t.encode("utf-16-le")
    for i in range(0, len(data), 2):
        h ^= data[i] | (data[i + 1] << 8)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


class Session:
    __slots__ = ("pid", "data", "path", "error")

    def __init__(self, pid: int, data: dict, path: Path, error: str | None = None):
        self.pid = pid
        self.data = data
        self.path = path
        self.error = error

    # --- производные поля ---
    @property
    def alive(self) -> bool:
        return pid_alive(self.pid)

    @property
    def heartbeat_age_s(self) -> float | None:
        hb = self.data.get("heartbeat")
        if not isinstance(hb, (int, float)):
            return None
        return max(0.0, (time.time() * 1000 - hb) / 1000.0)

    @property
    def running(self) -> dict | None:
        return self.data.get("running")

    def summary(self, stale_after: float) -> dict:
        """Краткое описание для list_sessions и выбора сессии."""
        age = self.heartbeat_age_s
        out = {
            "pid": self.pid,
            "alive": self.alive,
            "platform": self.data.get("platform"),
            "platformSupported": platform_supported(self.data.get("platform")),
            "conn": self.data.get("conn"),
            "containers": self.data.get("containers"),
            "running": self.running,
            "heartbeatAge_s": round(age, 1) if age is not None else None,
        }
        if age is not None and self.alive and age > stale_after:
            out["heartbeatStale"] = True  # мост занят долгой операцией (тиков нет)
        if self.error:
            out["error"] = self.error
        return out


class Bridge:
    """Клиент мостов всех сессий каталога <root>/sessions."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = Path(cfg.root)

    # ---------- реестр сессий ----------

    def sessions(self) -> list[Session]:
        out: list[Session] = []
        sdir = self.root / "sessions"
        if not sdir.is_dir():
            return out
        for entry in sorted(sdir.iterdir()):
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            sfile = entry / "session.json"
            if not sfile.is_file():
                continue
            data: dict = {}
            error = None
            try:
                data = json.loads(sfile.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as e:
                error = f"session.json не читается: {e}"
            out.append(Session(int(entry.name), data, sfile, error))
        return out

    def live_sessions(self) -> list[Session]:
        return [s for s in self.sessions() if s.alive]

    def pick_session(self, pid: int | None = None) -> int:
        """Выбрать сессию: по pid, иначе единственная живая. Иначе отказ со списком.

        Сессии на непроверенной платформе (≠8.3.27) не выбираются — движение по
        сдвинутым vtable отдаёт мусор; обход — allow_unverified_platform в конфиге.
        """
        all_live = self.live_sessions()  # один срез реестра (ревью 25.10.2026)
        allowed = self.cfg.allow_unverified_platform
        live = [s for s in all_live
                if allowed or platform_supported(s.data.get("platform"))]
        skipped = [s for s in all_live if s not in live]
        if pid is not None:
            for s in live:
                if s.pid == pid:
                    return pid
            if any(s.pid == pid for s in skipped):
                raise NoSessionError(
                    f"сессия {pid} на непроверенной платформе "
                    f"«{(next(s.data.get('platform') for s in skipped if s.pid == pid))}» — "
                    "поддерживается только 8.3.27 (обход: allow_unverified_platform)")
            have = ", ".join(str(s.pid) for s in live) or "нет живых"
            raise NoSessionError(f"сессия {pid} не жива; живые: {have}")
        if not live:
            if skipped:
                raise NoSessionError(
                    "живые сессии есть, но все на непроверенной платформе: "
                    + ", ".join(f"{s.pid} ({s.data.get('platform')})" for s in skipped)
                    + "; поддерживается только 8.3.27 (обход: allow_unverified_platform)")
            raise NoSessionError(
                "нет живых сессий: конфигуратор с мостом не запущен "
                "(нужны Снегопат starter.exe, файл включения mcp_bridge.on и открытый конфигуратор)")
        if len(live) == 1:
            return live[0].pid
        raise NoSessionError(
            "несколько живых сессий, укажите session (pid): "
            + ", ".join(f"{s.pid} ({(s.data.get('conn') or '?')[:60]})" for s in live))

    # ---------- вызов операции ----------

    def call(self, pid: int, op: str, args: dict | None = None,
             timeout: float | None = None) -> dict:
        """Выполнить операцию моста. Возвращает result (или бросает BridgeError).

        Идемпотентность на стороне моста по id; здесь id уникален на вызов.
        """
        if timeout is None:
            timeout = self.cfg.long_timeout_s if op in LONG_OPS else self.cfg.default_timeout_s
        rid = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        req = {"op": op, "args": args or {}, "deadline": int((time.time() + timeout) * 1000)}
        req_dir = self.root / "sessions" / str(pid) / "req"
        resp_path = self.root / "sessions" / str(pid) / "resp" / f"{rid}.json"
        if not req_dir.is_dir():
            raise NoSessionError(f"каталог сессии {pid} не существует (мост не инициализирован)")
        tmp = req_dir / f"{rid}.tmp"
        tmp.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, req_dir / f"{rid}.json")

        t0 = time.time()
        resp = None
        while time.time() - t0 < timeout:
            if resp_path.is_file():
                try:
                    resp = json.loads(resp_path.read_text(encoding="utf-8-sig"))
                    break  # разбор удался; файл ответа удаляем ПОСЛЕ использования
                except (OSError, json.JSONDecodeError):
                    pass  # недописанный ответ — подождать
            time.sleep(0.02)
        if resp is not None:
            try:
                resp_path.unlink(missing_ok=True)
            except OSError:
                pass  # файл удержан читателем — ответ уже разобран, это не таймаут
        else:
            self._journal(pid, op, False, time.time() - t0, "timeout")
            raise BridgeError(
                "timeout", f"нет ответа моста за {timeout:.0f} с (op={op}, id={rid}: "
                "перед повтором проверьте bridge_wait по этому id — операция могла "
                "завершиться позже; повторять только при состоянии unknown)",
                {"id": rid, "op": op})
        self._journal(pid, op, bool(resp.get("ok")), time.time() - t0,
                      (resp.get("error") or {}).get("code"))
        if not resp.get("ok"):
            err = resp.get("error") or {}
            raise BridgeError(err.get("code", "?"), err.get("message", "?"), err.get("where"))
        out = resp.get("result")
        if isinstance(out, dict):
            out.setdefault("_id", rid)
            out.setdefault("_ms", resp.get("ms"))
        return out

    # ---------- журнал ----------

    def _journal(self, pid: int, op: str, ok: bool, ms: float, code: str | None) -> None:
        if not self.cfg.journal_path:
            return
        try:
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": pid, "op": op,
                   "ok": ok, "ms": round(ms * 1000)}
            if code:
                rec["code"] = code
            self.cfg.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cfg.journal_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # журнал не должен ломать вызовы
