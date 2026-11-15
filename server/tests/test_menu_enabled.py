# -*- coding: utf-8 -*-
"""№29 (v1.13): menu_enabled — пиксельный детектор активности пунктов меню.

Проверка на РЕАЛЬНЫХ скриншотах выпавших меню конфигуратора
(Y:\\Snegopat-mcp\\probe\\menumap): config.png (кроп от (120,30), меню
«Конфигурация») и file.png (кроп от (0,30), меню «Файл»). Если образцов нет
на станции — тесты пропускаются (регресс не зависит от артефактов пробы).

Калибровка порога: на config.png известны выключенные строки («Открыть
конфигурацию» y≈39, «Сохранить конфигурацию» y≈92, «Загрузить конфигурацию
из файла…» y≈266, контраст 0.46..0.52) и активные («Обновить конфигурацию
базы данных» y≈119, «Расширения конфигурации» y≈175, контраст 0.80..0.85).
DEFAULT_THRESHOLD = 0.66 — калибровочная середина 0.6585, зафиксирована
здесь. Замечание: «Закрыть конфигурацию» (config.png y≈65) в ТЗ помечено
СЕРЫМ, но в образце отрисовано чёрным (проверено по пикселям) — в тесты
не входит.

Прогон: python -m unittest discover -s tests  (из server/, только discover).
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import menu_enabled  # noqa: E402
from menu_enabled import (  # noqa: E402
    DEFAULT_THRESHOLD,
    calibrate,
    row_enabled,
    scan_rows,
)

SAMPLE_DIR = Path(os.environ.get(
    "MENUMAP_SAMPLES", r"Y:\Snegopat-mcp\probe\menumap"))
CONFIG_PNG = SAMPLE_DIR / "config.png"
FILE_PNG = SAMPLE_DIR / "file.png"

HAVE_SAMPLES = CONFIG_PNG.exists() and FILE_PNG.exists()

# Краны строк (y в кропе), уточнённые по фактическим пикселям образцов.
CFG_DISABLED = [("disabled:Открыть конфигурацию", 39),
                ("disabled:Сохранить конфигурацию", 92),
                ("disabled:Загрузить конфигурацию из файла", 266)]
CFG_ENABLED = [("enabled:Обновить конфигурацию базы данных", 119),
               ("enabled:Конфигурация базы данных", 146),
               ("enabled:Расширения конфигурации", 175),
               ("enabled:Поддержка", 208),
               ("enabled:Сохранить конфигурацию в файл", 239)]
# file.png переснят 15.11 (полный кроп 0..760): y-центры по scan_rows;
# «Сохранить копию» СЕРЫЙ (первичная разметка ошиблась — детектор был прав)
FILE_ENABLED = [("enabled:Новый", 39),
                ("enabled:Открыть", 65),
                ("enabled:MRQ 1 (StndlEDO)", 347)]
FILE_DISABLED = [("disabled:Закрыть", 91.5),
                 ("disabled:Сохранить как", 123),
                 ("disabled:Сохранить копию", 150)]

# Зона иконок слева исключается: x0 правее начала текста (~x45 в кропе).
X0 = 60


@unittest.skipUnless(HAVE_SAMPLES, "нет образцов %s" % SAMPLE_DIR)
class TestRowEnabledOnSamples(unittest.TestCase):
    """row_enabled: выключенные/активные строки на обоих образцах."""

    def _check(self, png, cases, expected, x1=10 ** 6):
        for label, y in cases:
            with self.subTest(label=label, y=y):
                res = row_enabled(png, y, X0, x1)
                self.assertEqual(res["enabled"], expected,
                                 "contrast=%.3f" % res["contrast"])
                # контраст активных >= 0.70, выключенных <= 0.60
                # (фактические кластеры: 0.80..0.85 против 0.46..0.52)
                if expected:
                    self.assertGreaterEqual(res["contrast"], 0.70)
                else:
                    self.assertLessEqual(res["contrast"], 0.60)

    def test_config_disabled_rows(self) -> None:
        self._check(CONFIG_PNG, CFG_DISABLED, expected=False)

    def test_config_enabled_rows(self) -> None:
        self._check(CONFIG_PNG, CFG_ENABLED, expected=True)

    def test_file_enabled_rows(self) -> None:
        self._check(FILE_PNG, FILE_ENABLED, expected=True, x1=450)

    def test_file_disabled_rows(self) -> None:
        self._check(FILE_PNG, FILE_DISABLED, expected=False, x1=450)

    def test_pil_image_input(self) -> None:
        """Контракт: принимает и PIL Image, и путь — результат тот же."""
        from PIL import Image
        im = Image.open(CONFIG_PNG).convert("RGB")
        by_path = row_enabled(CONFIG_PNG, 119, X0, 1000)
        by_image = row_enabled(im, 119, X0, 1000)
        self.assertTrue(by_path["enabled"])
        self.assertEqual(by_path["contrast"], by_image["contrast"])

    def test_empty_band_disabled(self) -> None:
        """Синтетика: чистый фон без текста -> disabled, contrast 0."""
        from PIL import Image
        im = Image.new("RGB", (200, 60), (221, 242, 245))  # тон фона меню
        for y in (10, 30, 50):
            res = row_enabled(im, y, 0, 200)
            self.assertFalse(res["enabled"])
            self.assertEqual(res["contrast"], 0.0)


class TestSynthetic(unittest.TestCase):
    """Синтетика (без образцов): метрика contrast на черном/сером/фоне."""

    BG = (221, 242, 245)

    def _menu(self, core_rgb=None) -> "Image.Image":
        from PIL import Image
        im = Image.new("RGB", (200, 60), self.BG)
        if core_rgb is not None:
            for y in range(25, 36):          # текстовая строка ~11px
                for x in range(20, 120):
                    im.putpixel((x, y), core_rgb)
        return im

    def test_black_core_enabled(self) -> None:
        res = row_enabled(self._menu((0, 0, 0)), 30, 0, 200)
        self.assertTrue(res["enabled"])
        self.assertGreater(res["contrast"], 0.8)

    def test_gray_core_disabled(self) -> None:
        res = row_enabled(self._menu((118, 118, 118)), 30, 0, 200)
        self.assertFalse(res["enabled"])
        self.assertLess(res["contrast"], 0.6)

    def test_no_text_contrast_zero(self) -> None:
        res = row_enabled(self._menu(), 30, 0, 200)
        self.assertFalse(res["enabled"])
        self.assertEqual(res["contrast"], 0.0)


@unittest.skipUnless(HAVE_SAMPLES, "нет образцов %s" % SAMPLE_DIR)
class TestScanRowsOnSamples(unittest.TestCase):
    """scan_rows: координаты строк без OCR."""

    def test_config_finds_20_plus_rows(self) -> None:
        rows = scan_rows(CONFIG_PNG, X0, 1000, 30, 730)
        self.assertGreaterEqual(len(rows), 20)

    def test_config_rows_sorted_in_range(self) -> None:
        rows = scan_rows(CONFIG_PNG, X0, 1000, 30, 730)
        ys = [r["y_center"] for r in rows]
        self.assertEqual(ys, sorted(ys))
        for y in ys:
            self.assertGreaterEqual(y, 30)
            self.assertLess(y, 730)

    def test_config_known_row_centers(self) -> None:
        """Строки из ТЗ присутствуют: y≈39, ≈119, ≈175 (кроп config.png)."""
        ys = [r["y_center"] for r in scan_rows(CONFIG_PNG, X0, 1000, 30, 730)]
        for expected, tol in ((39, 4), (119, 4), (178, 6)):
            self.assertTrue(
                any(abs(y - expected) <= tol for y in ys),
                "нет строки y≈%d (±%d), найдено: %s" % (expected, tol, ys))

    def test_file_known_row_centers(self) -> None:
        ys = [r["y_center"] for r in scan_rows(FILE_PNG, X0, 1000, 30, 700)]
        self.assertGreaterEqual(len(ys), 15)
        for expected, tol in ((39, 4), (64, 4), (90, 4)):
            self.assertTrue(
                any(abs(y - expected) <= tol for y in ys),
                "нет строки y≈%d (±%d), найдено: %s" % (expected, tol, ys))

    def test_text_rows_height_sane(self) -> None:
        """Высота полосы — высота текста ~10-20px, не пол-меню."""
        rows = scan_rows(CONFIG_PNG, X0, 1000, 30, 730)
        for r in rows:
            self.assertGreaterEqual(r["text_rows"], 5)
            self.assertLessEqual(r["text_rows"], 24)

    def test_scan_and_row_enabled_agree(self) -> None:
        """Стыковка: scan_rows -> row_enabled на найденных центрах."""
        im = menu_enabled._as_image(CONFIG_PNG)  # noqa: SLF001 - контракт Image
        rows = scan_rows(im, X0, 1000, 30, 730)
        states = [row_enabled(im, r["y_center"], X0, 1000) for r in rows]
        # в config.png есть и серые, и чёрные строки
        self.assertTrue(any(s["enabled"] for s in states))
        self.assertTrue(any(not s["enabled"] for s in states))


@unittest.skipUnless(HAVE_SAMPLES, "нет образцов %s" % SAMPLE_DIR)
class TestCalibration(unittest.TestCase):
    """calibrate: порог между известными классами; DEFAULT_THRESHOLD."""

    def test_threshold_between_classes(self) -> None:
        rows = dict(CFG_DISABLED + CFG_ENABLED)
        cal = calibrate(CONFIG_PNG, rows)
        self.assertLess(cal["disabled_max"], cal["threshold"])
        self.assertLess(cal["threshold"], cal["enabled_min"])
        # разделение классов устойчивое
        self.assertGreaterEqual(
            cal["enabled_min"] - cal["disabled_max"], 0.15)

    def test_default_threshold_fixed_on_config(self) -> None:
        """DEFAULT_THRESHOLD (0.66) воспроизводится калибровкой на config.png."""
        cal = calibrate(CONFIG_PNG, dict(CFG_DISABLED + CFG_ENABLED))
        self.assertAlmostEqual(cal["threshold"], DEFAULT_THRESHOLD,
                               delta=0.05)
        # и на file.png тот же порог разделяет классы
        cal_file = calibrate(FILE_PNG, dict(FILE_DISABLED + FILE_ENABLED),
                         x1=450)
        self.assertLess(cal_file["disabled_max"], DEFAULT_THRESHOLD)
        self.assertGreater(cal_file["enabled_min"], DEFAULT_THRESHOLD)

    def test_bad_labels_rejected(self) -> None:
        with self.assertRaises(ValueError):
            calibrate(CONFIG_PNG, {"Открыть": 39})
        with self.assertRaises(ValueError):
            calibrate(CONFIG_PNG, {"enabled:Обновить": 119})  # один класс

    def test_module_threshold_constant(self) -> None:
        """Порог зафиксирован модульной константой (0.66)."""
        self.assertEqual(DEFAULT_THRESHOLD, 0.66)


if __name__ == "__main__":
    unittest.main()
