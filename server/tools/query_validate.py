# -*- coding: utf-8 -*-
"""Валидация запросов 1С через V83.COMConnector — без конфигуратора (IDEAS №21).

Язык запросов 1С — read-only (писать им нельзя), поэтому исполнение безопасно
по данным. Два режима:
- check (по умолчанию): запрос оборачивается в «ВЫБРАТЬ ПЕРВЫЕ 1 * ИЗ (<запрос>)
  КАК валидация» — синтаксис и метаданные проверяются, данных почти не читается;
  не работает для запросов с ИТОГИ/спец-конструкциями — для них режим execute;
- execute: запрос исполняется как есть, возвращаются имена колонок и до limit
  строк (тяжёлый запрос выполнится полностью — язык запросов не даёт таймаута).

Учётка/монопольность: см. eventlog_cfg — COM-соединение требует, чтобы база не
была занята конфигуратором; typelib comcntr поднимается per-user автоматически.
"""
import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I: каталог скрипта не в path


class QueryError(RuntimeError):
    pass


def wrap_first1(query: str) -> str:
    """Обёртка «ВЫБРАТЬ ПЕРВЫЕ 1 *» вокруг запроса для дешёвой валидации."""
    return "ВЫБРАТЬ ПЕРВЫЕ 1 * ИЗ (" + query.strip().rstrip(";") + ") КАК валидация"


def validate(base: str, query: str, mode: str = "check", limit: int = 5) -> dict:
    if not query or not query.strip():
        raise QueryError("query пуст")
    if mode not in ("check", "execute"):
        raise QueryError("mode: check|execute")
    from eventlog_cfg import _connect, EventLogError  # общий COM-контур
    try:
        conn = _connect(base)
    except EventLogError as e:
        raise QueryError(str(e))
    run_text = wrap_first1(query) if mode == "check" else query
    try:
        q = conn.NewObject("Запрос")
        q.Текст = run_text
        result = q.Выполнить()
    except Exception as e:
        # ошибка 1С = результат валидации: текст ошибки и есть вердикт
        return {"ok": False, "mode": mode, "error": str(e)[:500]}
    cols = []
    try:
        sel = result.Выбрать()
        for i in range(sel.КоличествоКолонок()):
            cols.append(str(sel.ИмяКолонки(i)) if hasattr(sel, "ИмяКолонки") else str(i))
    except Exception:
        pass
    rows = []
    try:
        sel = result.Выбрать()
        while sel.Следующий() and len(rows) < max(0, int(limit)):
            row = {}
            for name in cols or []:
                try:
                    row[name] = str(getattr(sel, name, ""))
                except Exception:
                    row[name] = ""
            rows.append(row)
    except Exception:
        pass
    return {"ok": True, "mode": mode, "columns": cols, "rows": rows,
            "note": "запрос выполнен (язык запросов read-only)"}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Валидация запроса 1С через COM")
    ap.add_argument("--base", required=True, help="путь файловой ИБ")
    ap.add_argument("--query", required=True, help="текст запроса или файл @path.txt")
    ap.add_argument("--mode", default="check", help="check|execute")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args(argv)
    query = args.query
    if query.startswith("@"):
        query = io.open(query[1:], encoding="utf-8-sig").read()
    try:
        res = validate(args.base, query, args.mode, args.limit)
    except QueryError as e:
        print("query_validate: %s" % e, file=sys.stderr)
        return 2
    except Exception as e:
        res = {"ok": False, "error": str(e)[:300]}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
