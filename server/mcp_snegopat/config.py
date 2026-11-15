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

# умолчание совпадает с аварийным умолчанием моста (v1.2); боевой путь всегда
# в конфиге — установщик пишет <дом>\mcp в mcp_snegopat.json
DEFAULT_ROOT = Path(r"C:\Snegopat-mcp\mcp")


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
    # №10: per-op таймауты (имя операции -> секунды); перекрывают LONG_OPS и
    # умолчание (резолюция: op_timeouts -> long -> default, см. bridge.resolve_timeout)
    op_timeouts: dict[str, float] = field(default_factory=dict)
    # №10: дополнительные долгие операции к встроенному LONG_OPS (НЕ замена —
    # объединяются; элементы, уже входящие в LONG_OPS, дублировать не нужно)
    long_ops: list[str] = field(default_factory=list)
    # heartbeat старше этого + живой pid = мост занят долгой операцией (не «мёртв»)
    heartbeat_stale_s: float = 30.0
    journal_path: Path | None = None  # умолчание: <root>/journal.jsonl
    # журнал регистрации (этап 18/19): настройки пишутся инструментом
    # journal_configure в <root>/mcp_bridge.cfg.json (их читает МОСТ) и
    # дублируются здесь (для md_journal: где искать и что показывать)
    journal_enabled: bool = True       # false = мост не пишет журнал
    journal_kinds: list[int] = field(default_factory=list)  # пусто = все виды
    journal_max_mb: float = 20.0       # ротация: размер файла
    journal_keep: int = 3              # ротация: сколько ротированных хранить
    # №20 (пресет «Analysis Only»): запрет операций записи в метаданные/БД
    # (модули, свойства, создание/удаление, config_save, update_db_cfg, формы,
    # хранилище); чтение и диагностика работают. Гейт на мостовых op в _call
    read_only: bool = False
    # №20 (остаток, v1.6.0): пресеты — виден только subset инструментов
    # (server.PRESETS: analysis/meta/forms). Пусто = все. Незнакомое имя =
    # отказ при старте (ValueError со списком доступных)
    presets: list[str] = field(default_factory=list)
    # №13/№27 (v1.7.0): MCP over HTTP (streamable-http). Умолчание — stdio,
    # HTTP включается ключом --http (CLI) или http_enabled в конфиге.
    # auth_token — Bearer-токен (№27); ПУСТОЙ ТОКЕН ПРИ НЕЛОКАЛЬНОМ СЛУШАТЕЛЕ =
    # ОТКАЗ ПРИ СТАРТЕ (безопасный дефолт: удалённая станция без auth запрещена)
    http_enabled: bool = False
    http_host: str = "127.0.0.1"        # 127.0.0.1 = только с этой машины
    http_port: int = 8641
    auth_token: str = ""


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
        elif key == "journal_kinds":
            if not isinstance(value, list) or not all(isinstance(v, int) for v in value):
                raise ValueError("journal_kinds должно быть списком целых")
            cfg.journal_kinds = value
        elif isinstance(cur, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} должно быть true/false")
            setattr(cfg, key, value)
        elif isinstance(cur, list):
            if not isinstance(value, list):
                raise ValueError(f"{key} должно быть списком")
            setattr(cfg, key, value)
        elif isinstance(cur, dict):
            # №10: словарные поля (op_timeouts) — ключи-строки, значения-числа
            if not isinstance(value, dict):
                raise ValueError(f"{key} должно быть словарём")
            for k, v in value.items():
                if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                    raise ValueError(f"{key}[{k}] должно быть положительным числом")
            setattr(cfg, key, {str(k): float(v) for k, v in value.items()})
        elif isinstance(cur, float):
            setattr(cfg, key, float(value))
        elif isinstance(cur, int) and not isinstance(cur, bool):
            # int-поля (journal_keep); bool — подтип int, отсечён веткой выше
            setattr(cfg, key, int(value))
        else:
            raise ValueError(f"неподдерживаемый тип ключа {key}")
