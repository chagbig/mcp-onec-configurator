# -*- coding: utf-8 -*-
"""Тесты №2 (search_live) и №4 (blog_publish): синтетика, без сети."""
from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


class TestSearchLive(unittest.TestCase):
    def test_find_needle_basic(self) -> None:
        from search_live import find_needle
        text = "Процедура А()\r\n\tх = РасчетСуммы(1);\r\nКонецПроцедуры\r\n\tу = РасчетСуммыНовая();\r\n"
        hits = find_needle(text, 'РасчетСуммы', False, 10)
        # РасчетСуммыНовая тоже содержит подстроку — это честный superset
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]['line'], 2)

    def test_find_needle_regex_digits(self) -> None:
        from search_live import find_needle
        text = 'Строка1\r\nСтрока 42\r\nСтрока333'
        hits = find_needle(text, r'Строка \d+', True, 10)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['line'], 2)

    def test_find_needle_max_hits(self) -> None:
        from search_live import find_needle
        text = 'РасчетСуммы(1)\r\nРасчетСуммы(2)\r\nРасчетСуммы(3)'
        hits = find_needle(text, 'РасчетСуммы', False, 2)
        self.assertEqual(len(hits), 2)


class TestSearchLiveLive(unittest.TestCase):
    """search_live на подставном мосте: модули объектов ищутся, формы —
    честная диагностика в note (модули форм живьём не сканируются)."""

    def setUp(self) -> None:
        import time
        import tempfile
        from mcp_snegopat.config import Config
        from mcp_snegopat.server import Toolkit
        from fake_bridge import FakeBridge
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-snegopat-test-")
        self.root = Path(self.tmp.name)
        self.fake = FakeBridge(self.root, pid=os.getpid()).start()
        self.cfg = Config(root=self.root, journal_path=self.root / "journal.jsonl")
        self.tk = Toolkit(self.cfg)
        for _ in range(300):
            if self.tk.bridge.live_sessions():
                break
            time.sleep(0.01)
        else:
            self.fail("подставной мост не поднялся за 3 с")

    def tearDown(self) -> None:
        self.fake.stop()
        self.tmp.cleanup()

    def test_object_module_hit(self) -> None:
        self.fake.kind_items["Справочники"] = [
            {"name": "Эмитенты", "cls": "Справочник", "id": "sr1"}]
        self.fake.modules[("Справочник.Эмитенты", "МодульОбъекта")] = (
            "Процедура А()\r\n\tneedle_here();\r\nКонецПроцедуры\r\n")
        from search_live import search_live
        res = search_live(self.tk, session=os.getpid(), container=0,
                          needle="needle_here")
        self.assertEqual(res["modules_scanned"], 1)
        self.assertEqual(len(res["matches"]), 1)
        self.assertEqual(res["matches"][0]["object"], "Справочник.Эмитенты")
        self.assertEqual(res["matches"][0]["prop"], "МодульОбъекта")
        self.assertEqual(res["matches"][0]["hits"][0]["line"], 2)

    def test_forms_note_honest(self) -> None:
        from search_live import search_live
        res = search_live(self.tk, session=os.getpid(), container=0,
                          needle="что-угодно")
        self.assertEqual(res["modules_scanned"], 0)
        self.assertIn("МОДУЛИ ФОРМ", res["note"])
        self.assertIn("md_search_code", res["note"])
        self.assertNotIn("include_forms", res)  # никаких выдуманных механизмов


class TestBlogPublish(unittest.TestCase):
    def test_slugify_cyrillic(self) -> None:
        from blog_publish import slugify
        s = slugify('Агент работает в живом конфигураторе')
        self.assertTrue(s.startswith('agent-rabotaet'))
        self.assertNotIn('а', s)
        self.assertFalse(s.endswith('-'))

    def test_slugify_empty_raises(self) -> None:
        from blog_publish import slugify, BlogError
        with self.assertRaises(BlogError):
            slugify('!!!')

    def test_md_to_html_headings_lists(self) -> None:
        from blog_publish import md_to_html
        md = ('# Заголовок поста\n'
              '## Раздел\n'
              'текст с **жирным** и [ссылкой](https://x.y)\n'
              '- пункт один\n'
              '- пункт два\n')
        h = md_to_html(md)
        self.assertIn('<h2>Раздел</h2>', h)
        self.assertIn('<strong>жирным</strong>', h)
        self.assertIn('<a href="https://x.y">ссылкой</a>', h)
        self.assertIn('<li>пункт один</li>', h)
        self.assertNotIn('<h1>', h)  # # — это title, не в контенте
        self.assertNotIn('# Заголовок', h)

    def test_md_to_html_code_block(self) -> None:
        from blog_publish import md_to_html
        h = md_to_html('```\nАгент\n  │ мост\n```')
        self.assertIn('<pre><code>', h)
        self.assertIn('Агент', h)
        self.assertIn('</code></pre>', h)

    def test_publish_requires_file(self) -> None:
        from blog_publish import publish, BlogError
        with self.assertRaises(BlogError):
            publish(r'C:\нет\статьи.md', title='Т')

    def test_publish_requires_creds(self) -> None:
        from blog_publish import publish, BlogError
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            md = Path(d) / 'a.md'
            md.write_text('# Т', encoding='utf-8')
            env = Path(d) / 'env.env'
            env.write_text('WP_USER=u\n', encoding='utf-8')
            with self.assertRaises(BlogError) as cm:
                publish(str(md), env_path=str(env))
            self.assertIn('WP_USER/WP_APP_PASS', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
