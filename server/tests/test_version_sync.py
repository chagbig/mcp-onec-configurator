# -*- coding: utf-8 -*-
"""№28/#23 (прогон v1.17): единая истина версии — __init__.__version__ и
pyproject.toml обязаны совпадать; счётчик MCP-инструментов в _TOOLS >= 70.
Упало = релиз забыл бамп (как __version__=1.15.0 в v1.17)."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestVersionSync(unittest.TestCase):
    def test_init_matches_pyproject(self) -> None:
        from mcp_snegopat import __version__
        py = Path(__file__).resolve().parents[1] / "pyproject.toml"
        m = re.search(r'version = "([^"]+)"', py.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(m, "pyproject: нет version=")
        self.assertEqual(__version__, m.group(1),
                         "__version__ и pyproject расходятся — релизный бамп забыт")

    def test_tool_count_reported(self) -> None:
        # счётчик для доков: версия кода -> фактическое число MCP-инструментов
        from mcp_snegopat.server import _TOOLS
        self.assertGreaterEqual(len(_TOOLS), 70)
        # версия не отстаёт от известной линии релизов
        from mcp_snegopat import __version__
        parts = [int(x) for x in __version__.split(".")]
        self.assertGreaterEqual(parts[0] * 100 + parts[1], 118,
                                "версия не бампнута для v1.18")


if __name__ == "__main__":
    unittest.main()
