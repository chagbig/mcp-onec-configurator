# -*- coding: utf-8 -*-
"""Внутренний текст формы 1С: чтение, шапка-нормализатор, сравнение (этап 8).

Карта отличий «cf-текст vs form_save-текст» снята в П1 (27.10.2026,
docs/stage-08-forms.md §4.1): тексты совпадают всюду, кроме поля имени активного
элемента в шапке редактора — строка вида

    {-1,02023637-7868-4a5f-8576-835a76e0c9ba},0,0,0,9,"ФормаКоманднаяПанель",

где в выгрузке из открытого редактора (form_save) стоит имя выбранного элемента,
а в записи cf — пусто. 02023637-… — фиксированный гвид типа «форма» платформы.
Нормализатор очищает это поле; после него cf-текст и form_save-текст байт-в-байт
равны (проверено round-trip'ом form_load→form_save на empty2 и на форме
Номенклатура mockup-gm, 29 202 строки).
"""
import re

_ACTIVE_RE = re.compile(
    r'(\{-1,02023637-7868-4a5f-8576-835a76e0c9ba\},0,0,0,9,)"[^"]*"')


def normalize_form_text(text):
    """Убрать состояние редактора из шапки внутреннего текста формы."""
    return _ACTIVE_RE.sub(r'\1""', text)


def load_form_text(path):
    """Внутренний текст формы из файла (UTF-8 BOM как пишет saveToFile)."""
    raw = open(path, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8")


def _norm_eol(text):
    """Тексты форм смешивают CRLF и одиночные CR (строки событий) — выравниваем."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def texts_equal(a, b):
    """Сравнение двух текстов формы после нормализации шапки и переводов строк."""
    return _norm_eol(normalize_form_text(a)) == _norm_eol(normalize_form_text(b))


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3:
        t1 = load_form_text(sys.argv[1])
        t2 = load_form_text(sys.argv[2])
        eq = texts_equal(t1, t2)
        print("равны после нормализации:", eq)
        sys.exit(0 if eq else 1)
    print("usage: form_text.py <text1> <text2>", file=sys.stderr)
    sys.exit(2)
