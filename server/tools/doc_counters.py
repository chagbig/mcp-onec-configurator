# -*- coding: utf-8 -*-
"""№28 (прогон v1.17): автоподстановка счётчиков инструментов и версии в docи.

Счётчики в md-руководствах и титулы обновляются руками и отстают от кода
(70/71/74 расходились). Этот модуль даёт ЕДИНУЮ ИСТИНУ из кода:

- count_tools() -> (mcp, cli) — из _TOOLS сервера + фиксированный реестр CLI;
- patch_counters(md_text) -> текст с заменой «Инструменты (N: …)» и титула
  «v1.2.0»-подобных версий на актуальную;
- CLI: --check — сравнить факт с дока (rc=1 если расходятся — приёмка перед
  сборкой поставки); без --check — переписать файлы.

CLI-инструменты реестра — константа CLI_TOOLS (обновлять при добавлении).
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: CLI-инструменты (вне MCP; ручной реестр — их не видно в _TOOLS)
CLI_TOOLS = ["renovate_ext.py", "cf_arbiter.py", "uia_value_probe.py"]

#: доки, подлежащие правке (относительно корня репо)
DOC_FILES = ["РУКОВОДСТВО-ПОЛЬЗОВАТЕЛЯ.md", "РУКОВОДСТВО-АДМИНИСТРАТОРА.md"]


def count_tools() -> "tuple[int, int]":
    """(MCP-инструментов, CLI) из кода. Импорт server.py тяжёлый, но
    детерминированный; для CLI допустимо."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from mcp_snegopat.server import _TOOLS  # noqa: E402
    return len(_TOOLS), len(CLI_TOOLS)


def current_version() -> str:
    from mcp_snegopat import __version__  # noqa: E402
    return __version__


def patch_counters(text: str) -> "tuple[str, list[str]]":
    """Заменить в md: заголовок раздела «Инструменты (N: X MCP + Y CLI) — по
    группам», титул «Руководство … — mcp-configurator vX.Y.Z» и титул
    «ПОСТАВКА … vX.Y.Z». Возвращает (текст, список замен для отчёта)."""
    mcp, cli = count_tools()
    ver = current_version()
    changes: list[str] = []

    def _sub_counter(m: "re.Match") -> str:
        new = "Инструменты (%d: %d MCP + %d CLI)" % (mcp + cli, mcp, cli)
        if m.group(0) != new:
            changes.append("%s -> %s" % (m.group(0), new))
        return new

    text = re.sub(r"Инструменты \((\d+(?::[^)—]*)?)\)", _sub_counter, text)

    def _sub_title(m: "re.Match") -> str:
        new = m.group(1) + " v" + ver
        if m.group(0) != new:
            changes.append("%s -> %s" % (m.group(0), new))
        return new

    text = re.sub(r"(Руководство [а-яё ]+ — mcp-configurator) v[\d.]+",
                  _sub_title, text)
    text = re.sub(r"(ПОСТАВКА mcp-configurator) v[\d.]+", _sub_title, text)
    return text, changes


def main(argv: "list | None" = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Счётчики инструментов/версии в доках из кода")
    ap.add_argument("--check", action="store_true",
                    help="только сверка (rc=1 при расхождении), без записи")
    ap.add_argument("--files", nargs="*", default=DOC_FILES,
                    help="список md (по умолчанию оба руководства)")
    args = ap.parse_args(argv)
    mcp, cli = count_tools()
    print("код: %d MCP + %d CLI = %d; версия %s"
          % (mcp, cli, mcp + cli, current_version()))
    rc = 0
    for rel in args.files:
        p = REPO / rel
        if not p.is_file():
            print("нет файла:", p)
            rc = 2
            continue
        text = io.open(p, encoding="utf-8").read()
        new, changes = patch_counters(text)
        if changes:
            if args.check:
                print("РАСХОЖДЕНИЕ в %s:" % rel)
                for c in changes:
                    print("   ", c)
                rc = 1
            else:
                io.open(p, "w", encoding="utf-8", newline="\n").write(new)
                print("обновлён %s:" % rel)
                for c in changes:
                    print("   ", c)
        else:
            print("%s: актуален" % rel)
    return rc


if __name__ == "__main__":
    sys.exit(main())
