# -*- coding: utf-8 -*-
"""№31 (v1.19): карты меню per-платформа — menu_map.<ключ>.json выбирается
по платформе сессии (первые 3 сегмента версии); fallback menu_map.json.
Ловушка 17.11: на 8.5 headers_px сместились — menu_invoke по 8.3-карте
открыл «Настройки Снегопата»."""
from __future__ import annotations

import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


class TestMenuMapPlatform(unittest.TestCase):
    def setUp(self) -> None:
        from mcp_snegopat.config import Config
        from mcp_snegopat.server import Toolkit
        from fake_bridge import FakeBridge
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-mapplat-")
        self.root = Path(self.tmp.name)
        self.fake = FakeBridge(self.root, pid=os.getpid()).start()
        self.cfg = Config(root=self.root, journal_path=self.root / "journal.jsonl")
        self.cfg.allow_unverified_platform = True  # 8.5 вне allowed_platforms
        self.tk = Toolkit(self.cfg)
        for _ in range(300):
            if self.tk.bridge.live_sessions():
                break
            time.sleep(0.01)
        else:
            self.fail("подставной мост не поднялся за 3 с")
        # базовая карта (8.3.27): headers_px config=190
        base = {"headers_px": {"config": 190},
                "Конфигурация": [{"name": "Открыть конфигурацию", "enabled": True}]}
        (self.root / "menu_map.json").write_text(
            json.dumps(base, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.fake.stop()
        self.tmp.cleanup()

    def test_fallback_base_map_for_8_3(self) -> None:
        # платформа 8.3.27.2074 (дефолт fake) — платформенной карты нет → base
        p = self.tk._menu_map_path(session=os.getpid())
        self.assertEqual(p, self.root / "menu_map.json")

    def test_platform_map_selected_for_8_5(self) -> None:
        # сессия переезжает на 8.5.1.1621 → платформенная карта 8.5.1
        plat = {"headers_px": {"config": 233},
                "Конфигурация": [{"name": "Открыть конфигурацию", "enabled": True}]}
        (self.root / "menu_map.8.5.1.json").write_text(
            json.dumps(plat, ensure_ascii=False), encoding="utf-8")
        self.fake.platform = "8.5.1.1621"
        self.fake.heartbeat()  # переписать session.json
        for _ in range(50):
            if json.loads((self.root / "sessions" / str(os.getpid()) /
                           "session.json").read_text(encoding="utf-8"))["platform"] == "8.5.1.1621":
                break
            time.sleep(0.02)
        p = self.tk._menu_map_path(session=os.getpid())
        self.assertEqual(p, self.root / "menu_map.8.5.1.json")
        card = json.loads(p.read_text(encoding="utf-8-sig"))
        self.assertEqual(card["headers_px"]["config"], 233)

    def test_no_session_falls_back(self) -> None:
        p = self.tk._menu_map_path(session=999999)  # мёртвый pid — base
        self.assertEqual(p, self.root / "menu_map.json")

    def test_menu_invoke_uses_platform_headers(self) -> None:
        # menu_invoke берёт headers_px из ПЛАТФОРМЕННОЙ карты (координата
        # клика по заголовку «Конфигурация» = 233 на 8.5, не 190)
        plat = {"headers_px": {"config": 233},
                "Конфигурация": [{"name": "Открыть конфигурацию", "enabled": True}]}
        (self.root / "menu_map.8.5.1.json").write_text(
            json.dumps(plat, ensure_ascii=False), encoding="utf-8")
        self.fake.platform = "8.5.1.1621"
        self.fake.heartbeat()
        time.sleep(0.1)
        try:
            self.tk.menu_invoke(session=os.getpid(), menu="Конфигурация",
                                item="Открыть конфигурацию", shot=False)
        except Exception:
            pass  # fake-мост не исполняет меню — важно, что карта выбрана
        clicks = [args for op, args in self.fake.args_seen if op == "mouse_click"]
        self.assertTrue(clicks, "клик по заголовку не отправлен")
        # fake-мост без control_rect → fallback-ветка menu_x = header_x - 7:
        # коэффициент берётся из ПЛАТФОРМЕННОЙ карты (233, не 190)
        self.assertEqual(clicks[-1]["x"], 233 - 7)


if __name__ == "__main__":
    unittest.main()
