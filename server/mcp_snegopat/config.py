"""Конфигурация сервера mcp-snegopat.

Файл JSON ищется по порядку:
  1. переменная окружения MCP_SNEGOPAT_CONFIG (путь к файлу);
  2. mcp_snegopat.json в текущем каталоге;
  3. %LOCALAPPDATA%\\mcp-snegopat\\config.json.
Без файла — безопасные умолчания: запись хранилища и обновление БД запрещены.

Пример:
  {
    "root": "Y:\\Snegopat-mcp\\mcp",
    "own_bases": ["File=\"Y:\\Snegopat-mcp\\bases\\empty_mcp\";"],
    "allow_store": false,
    "allow_update_db": false
  }
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = Path(r"Y:\Snegopat-mcp\mcp")


@dataclass
class Config:
    # каталог моста: <root>/mcp_bridge.on, <root>/sessions/<pid>/{req,resp,session.json}
    root: Path = DEFAULT_ROOT
    # «свои» базы для update_db_cfg (инвариант 5); пустой список — отказ всегда
    own_bases: list[str] = field(default_factory=list)
    # операции хранилища (cfgstore_capture / cfgstore_commit)
    allow_store: bool = False
    # обновление конфигурации БД
    allow_update_db: bool = False
    # работать с сессиями на платформе вне 8.3.27 (сдвиг vtable — на свой риск)
    allow_unverified_platform: bool = False
    # заголовки диалогов, которые update_db_cfg закрывает «ОК» (подстрока, без учёта регистра)
    update_db_allowed_dialogs: list[str] = field(
        default_factory=lambda: ["реорганизация информации", "принять"])
    default_timeout_s: float = 120.0
    long_timeout_s: float = 900.0
    # heartbeat старше этого + живой pid = мост занят долгой операцией (не «мёртв»)
    heartbeat_stale_s: float = 30.0
    journal_path: Path | None = None  # умолчание: <root>/journal.jsonl


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Загрузить конфигурацию; отсутствующий файл — умолчания (без ошибки)."""
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    if os.environ.get("MCP_SNEGOPAT_CONFIG"):
        candidates.append(Path(os.environ["MCP_SNEGOPAT_CONFIG"]))
    candidates.append(Path("mcp_snegopat.json"))
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.append(Path(localappdata) / "mcp-snegopat" / "config.json")

    cfg = Config()
    for cand in candidates:
        if cand.is_file():
            data = json.loads(cand.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError(f"конфиг {cand} должен быть JSON-объектом")
            _apply(cfg, data)
            cfg.journal_path = cfg.journal_path or (cfg.root / "journal.jsonl")
            return cfg
    cfg.journal_path = cfg.root / "journal.jsonl"
    return cfg


def _apply(cfg: Config, data: dict) -> None:
    known = {f: f for f in cfg.__dataclass_fields__}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"неизвестный ключ конфига: {key}")
        cur = getattr(cfg, key)
        if key == "root":
            cfg.root = Path(value)
        elif key == "journal_path":
            cfg.journal_path = Path(value) if value else None
        elif isinstance(cur, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} должно быть true/false")
            setattr(cfg, key, value)
        elif isinstance(cur, list):
            if not isinstance(value, list):
                raise ValueError(f"{key} должно быть списком")
            setattr(cfg, key, value)
        elif isinstance(cur, float):
            setattr(cfg, key, float(value))
        else:
            raise ValueError(f"неподдерживаемый тип ключа {key}")
