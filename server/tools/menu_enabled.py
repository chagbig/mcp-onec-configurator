# -*- coding: utf-8 -*-
"""№29 (v1.13): пиксельный детектор активности пунктов меню конфигуратора.

Карта меню (menu_map.json) наполняется агентом-мультимодалом по скриншотам;
этот модуль даёт КОДОВУЮ проверку: по PNG выпавшего меню определяет, какие
строки АКТИВНЫ (текст чёрный, ~lum 36), какие DISABLED (текст светло-серый,
~lum 118), и y-центры строк — без OCR.

Физика (калибровка на реальных скриншотах Y:\\Snegopat-mcp\\probe\\menumap,
конфигурация 2 ГБ, стандартный масштаб Windows):
  - фон меню светло-голубой, яркость ~236..246 (0.299R+0.587G+0.114B);
  - ядро активного текста ~lum 36, выключенного ~lum 118; подсветка строки
    под курсором ~lum 205 (легко отделить порогом «темнее фона на 60»);
  - высота текста ~11 px, межстрочный шаг ~25-26 px, слева иконки ~x 10..35.

Контракт: функции принимают PIL Image или путь; только stdlib + Pillow —
кроссплатформенно (Windows/Linux). Порядок работы:
  rows = scan_rows(png, 60, ширина, 30, высота-40)   # координаты строк
  row_enabled(png, rows[i]["y_center"], 60, ширина)  # активность строки
  calibrate(png, {"disabled:Открыть конфигурацию": 39, ...})  # порог
Порог DEFAULT_THRESHOLD = 0.66 вычислен calibrate() на config.png
(выключенные: контраст 0.46..0.52, активные: 0.80..0.85, середина 0.6575)
и зафиксирован тестами tests/test_menu_enabled.py.
"""
from __future__ import annotations

import statistics
from pathlib import Path
from typing import Union

from PIL import Image

# --- Константы калибровки (не менять без прогона calibrate на образцах) ---

#: Порог контраста «активен/выключен»: калибровочная середина между
#: выключенным текстом (макс. контраст 0.520) и активным (мин. 0.795)
#: на config.png = 0.6575 (на file.png 0.6866); зафиксировано как 0.66 —
#: внутри обоих диапазонов, см. tests/test_menu_enabled.py.
DEFAULT_THRESHOLD = 0.66

#: Пиксель считается ТЕКСТОМ, если он темнее локального фона ярче чем на это
#: значение (0..255). Отсекает подсветку строки под курсором (дельта ~41)
#: и лёгкий антиалиасинг, оставляя ядра чёрного (дельта ~210) и серого
#: (дельта ~128) текста.
TEXT_DELTA = 60

#: То же расстояние для построчной плотности при поиске строк (scan_rows).
SCAN_DELTA = 60

#: Полоса вокруг y_center при оценке активности строки, px (текст ~11 px).
ROW_HALF = 10

#: Число верхних строк области для оценки фона меню (медиана), px.
TOP_BAND_ROWS = 8

#: Минимальная плотность «тёмных» пикселей в строке растра, чтобы строка
#: считалась частью текстовой полосы (гасит шум межстрочных зазоров).
MIN_DENSITY = 2

#: Максимальный зазор между соседними полосами низкой плотности (спуск
#: букв «руфз», верхние штрихи) при склейке одной текстовой строки, px.
MERGE_GAP = 4

#: Полоса с плотностью >= доля ширины области — это граница/разделитель
#: меню, а не текст: исключается из scan_rows.
SEPARATOR_RATIO = 0.5

#: x0 по умолчанию для calibrate: правее зоны иконок (~x 10..35 в кропе).
ICON_SAFE_X = 60

ImageLike = Union[Image.Image, str, Path]


# --- Вспомогательные ---

def _as_image(png: ImageLike) -> Image.Image:
    """PIL Image как есть; путь — открыть и привести к RGB."""
    if isinstance(png, Image.Image):
        return png if png.mode == "RGB" else png.convert("RGB")
    return Image.open(png).convert("RGB")


def _luminance(px_color) -> float:
    """Яркость пикселя (ITU-R 601), 0..255."""
    return 0.299 * px_color[0] + 0.587 * px_color[1] + 0.114 * px_color[2]


def _background_luminance(im: Image.Image, x0: int, x1: int, y0: int) -> float:
    """Медиана яркости верхней полосы области — фон меню известного тона."""
    px = im.load()
    y1 = min(y0 + TOP_BAND_ROWS, im.size[1])
    x1 = min(x1, im.size[0])
    vals = []
    for y in range(max(0, y0), y1):
        for x in range(max(0, x0), x1):
            vals.append(_luminance(px[x, y]))
    return statistics.median(vals)


# --- Публичный контракт ---

def scan_rows(png: ImageLike, x0: int, x1: int, y0: int, y1: int,
              min_density: int = MIN_DENSITY) -> list:
    """Найти y-центры строк текста в области, без OCR.

    Построчная плотность «тёмных относительно фона» пикселей (дельта
    SCAN_DELTA от медианы верхней полосы); полосы плотности >= min_density
    склеиваются через зазоры <= MERGE_GAP px; полосы, чья пиковая плотность
    накрывает >= SEPARATOR_RATIO ширины области — границы/разделители меню —
    выбрасываются.

    Возврат: [{"y_center": float, "text_rows": int, "density": int}, ...]
    по возрастанию y_center; text_rows — высота полосы в px.
    """
    im = _as_image(png)
    px = im.load()
    y0 = max(0, y0)
    y1 = min(y1, im.size[1])
    x1 = min(x1, im.size[0])
    bg = _background_luminance(im, x0, x1, y0)
    dens = []
    for y in range(y0, y1):
        n = 0
        for x in range(x0, x1):
            if bg - _luminance(px[x, y]) > SCAN_DELTA:
                n += 1
        dens.append(n)
    bands = []
    i, n_all = 0, len(dens)
    while i < n_all:
        if dens[i] >= min_density:
            j = i
            while True:
                # ищем следующую плотную строку через зазор <= MERGE_GAP
                k, gap = j + 1, 0
                while k < n_all and dens[k] < min_density and gap <= MERGE_GAP:
                    k += 1
                    gap += 1
                if k < n_all and dens[k] >= min_density and gap <= MERGE_GAP:
                    j = k
                else:
                    break
            bands.append((i, j, max(dens[i:j + 1])))
            i = j + 1
        else:
            i += 1
    out = []
    width = max(1, x1 - x0)
    for a, b, peak in bands:
        if peak >= SEPARATOR_RATIO * width:
            continue  # граница/разделитель меню
        out.append({"y_center": y0 + (a + b) / 2.0,
                    "text_rows": b - a + 1,
                    "density": peak})
    return out


def row_enabled(png: ImageLike, y_center: float, x0: int, x1: int,
                threshold: float = DEFAULT_THRESHOLD) -> dict:
    """Активность одного пункта: {enabled, contrast, ...}.

    Полоса y_center±ROW_HALF; локальный фон — медиана яркости полосы
    (учитывает подсвеченную строку под курсором); пиксели текста — темнее
    фона на > TEXT_DELTA; contrast = (фон − средний текст) / фон;
    enabled = contrast >= threshold (DEFAULT_THRESHOLD, см. calibrate).
    Пустая полоса/нет текста -> {enabled: False, contrast: 0.0}.
    """
    im = _as_image(png)
    px = im.load()
    ya = max(0, int(y_center) - ROW_HALF)
    yb = min(im.size[1], int(y_center) + ROW_HALF + 1)
    xa = max(0, x0)
    xb = min(x1, im.size[0])
    vals = []
    for y in range(ya, yb):
        for x in range(xa, xb):
            vals.append(_luminance(px[x, y]))
    if not vals:
        return {"enabled": False, "contrast": 0.0}
    bg = statistics.median(vals)
    text = [v for v in vals if bg - v > TEXT_DELTA]
    if not text:
        return {"enabled": False, "contrast": 0.0, "bg": bg}
    mean = sum(text) / len(text)
    contrast = (bg - mean) / bg
    return {"enabled": contrast >= threshold,
            "contrast": contrast,
            "bg": bg,
            "text_pixels": len(text)}


def calibrate(png: ImageLike, rows: dict,
              x0: int = ICON_SAFE_X, x1: int = None) -> dict:
    """Вычислить порог между известными чёрными и серыми метками.

    rows: {label: y_center}, метки с префиксом "enabled:"/"disabled:"
    (например {"disabled:Открыть конфигурацию": 39,
               "enabled:Обновить конфигурацию базы данных": 119}).
    Порог = середина между максимальным контрастом выключенных и
    минимальным контрастом активных. Возврат:
    {"threshold", "disabled_max", "enabled_min", "contrasts": {...}}.
    Значение, вычисленное на config.png+file.png, зафиксировано в
    DEFAULT_THRESHOLD и тестами tests/test_menu_enabled.py.
    """
    im = _as_image(png)
    x1_eff = im.size[0] if x1 is None else x1
    contrasts = {}
    for label, y in rows.items():
        prefix, sep, _ = label.partition(":")
        if prefix not in ("enabled", "disabled") or not sep:
            raise ValueError(
                "метка должна быть 'enabled:имя' или 'disabled:имя': %r" % label)
        contrasts[label] = row_enabled(im, y, x0, x1_eff)["contrast"]
    dis = [c for lbl, c in contrasts.items() if lbl.startswith("disabled:")]
    ena = [c for lbl, c in contrasts.items() if lbl.startswith("enabled:")]
    if not dis or not ena:
        raise ValueError("нужны метки обоих классов: enabled:* и disabled:*")
    dis_max, ena_min = max(dis), min(ena)
    if dis_max >= ena_min:
        raise ValueError("классы не разделяются: disabled_max=%.3f >= "
                         "enabled_min=%.3f" % (dis_max, ena_min))
    return {"threshold": (dis_max + ena_min) / 2.0,
            "disabled_max": dis_max,
            "enabled_min": ena_min,
            "contrasts": contrasts}
