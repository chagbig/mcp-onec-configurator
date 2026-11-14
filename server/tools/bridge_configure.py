# -*- coding: utf-8 -*-
"""№10: управление конфигурацией моста/сервера (IDEAS №10, расширение механики
journal_configure).

Пишет ДВА файла:
  1. конфиг сервера (mcp_snegopat.json): default_timeout_s, long_timeout_s,
     heartbeat_stale_s, op_timeouts, long_ops — все существующие ключи
     сохраняются, файл не пересоздаётся. Эти поля python-процесс читает при
     старте: если сервер уже запущен — нужен перезапуск сервера (runtime-мутация
     живого экземпляра Config — забота интеграции).
  2. конфиг моста (<root>\\mcp_bridge.cfg.json, ключ cfg_cmd_whitelist на
     верхнем уровне): мост перечитывает по mtime в readBridgeCfg — применяется
     БЕЗ перезапуска конфигуратора. journal-секция не трогается; файл при
     отсутствии создаётся. Путь: bridge_cfg_path или <root> из конфига сервера.
"""
from __future__ import annotations

import json
from pathlib import Path

_TIMEOUT_FIELDS = ("default_timeout_s", "long_timeout_s", "heartbeat_stale_s")

_NOTE = ("серверные поля применяются при старте сервера (python-процесс читает "
         "конфиг один раз) — при запущенном сервере нужен перезапуск; "
         "cfg_cmd_whitelist мост применяет сам, без перезапуска конфигуратора "
         "(перечитывает mcp_bridge.cfg.json по mtime)")


def _pos_num(name: str, v) -> float:
    """Положительное число (таймауты)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise ValueError(f"{name} должно быть положительным числом, получено {v!r}")
    return float(v)


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} должен быть JSON-объектом")
    return data


def configure(cfg_path, bridge_cfg_path=None,
              default_timeout_s=None, long_timeout_s=None, heartbeat_stale_s=None,
              op_timeouts=None, long_ops=None, cfg_cmd_whitelist=None) -> dict:
    """Обновить конфиг сервера и/или белый список команд моста. Возвращает
    применённые значения + note о перезапуске."""
    applied: dict = {}
    server_fields = any(v is not None for v in (default_timeout_s, long_timeout_s,
                                                heartbeat_stale_s, op_timeouts,
                                                long_ops))
    if cfg_path is None and server_fields:
        raise ValueError("cfg_path обязателен при задании серверных полей "
                         "(путь к mcp_snegopat.json)")

    # --- валидация аргументов ---
    for name, v in (("default_timeout_s", default_timeout_s),
                    ("long_timeout_s", long_timeout_s),
                    ("heartbeat_stale_s", heartbeat_stale_s)):
        if v is not None:
            applied[name] = _pos_num(name, v)
    if op_timeouts is not None:
        if not isinstance(op_timeouts, dict):
            raise ValueError("op_timeouts должно быть словарём {op: секунды}")
        for op, t in op_timeouts.items():
            _pos_num(f"op_timeouts[{op!r}]", t)
        applied["op_timeouts"] = {str(op): float(t) for op, t in op_timeouts.items()}
    if long_ops is not None:
        if not isinstance(long_ops, list) or \
                not all(isinstance(o, str) and o.strip() for o in long_ops):
            raise ValueError("long_ops должно быть списком имён операций")
        applied["long_ops"] = list(long_ops)
    if cfg_cmd_whitelist is not None:
        if not isinstance(cfg_cmd_whitelist, list) or \
                not all(isinstance(n, int) and not isinstance(n, bool) and n > 0
                        for n in cfg_cmd_whitelist):
            raise ValueError("cfg_cmd_whitelist должно быть списком положительных целых "
                             "(номера команд CFG)")
        applied["cfg_cmd_whitelist"] = list(cfg_cmd_whitelist)

    cfg_file = Path(cfg_path) if cfg_path is not None else None
    data = _read_json(cfg_file) if cfg_file is not None else {}

    # --- конфиг сервера: обновить поля, остальные ключи сохранить ---
    for name in _TIMEOUT_FIELDS:
        if name in applied:
            data[name] = applied[name]
    if "op_timeouts" in applied:
        data["op_timeouts"] = dict(applied["op_timeouts"])
    if "long_ops" in applied:
        data["long_ops"] = list(applied["long_ops"])
    if applied and cfg_file is not None:
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                            encoding="utf-8")

    # --- конфиг моста: cfg_cmd_whitelist на верхнем уровне ---
    bridge_file: Path | None = None
    if cfg_cmd_whitelist is not None:
        if bridge_cfg_path:
            bridge_file = Path(bridge_cfg_path)
        else:
            root = data.get("root")
            if not root:
                raise ValueError("не задан bridge_cfg_path и в конфиге сервера "
                                 "нет ключа root — куда писать mcp_bridge.cfg.json?")
            bridge_file = Path(str(root)) / "mcp_bridge.cfg.json"
        bdata = _read_json(bridge_file)
        bdata["cfg_cmd_whitelist"] = list(cfg_cmd_whitelist)
        bridge_file.parent.mkdir(parents=True, exist_ok=True)
        bridge_file.write_text(json.dumps(bdata, ensure_ascii=False, indent=1),
                               encoding="utf-8")

    return {"applied": applied,
            "server_cfg": str(cfg_file),
            "bridge_cfg": str(bridge_file) if bridge_file else None,
            "note": _NOTE}
