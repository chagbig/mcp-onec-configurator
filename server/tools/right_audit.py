# -*- coding: utf-8 -*-
"""№34 (v1.20; идея #185): матрица доступов по ролям через COM-контур.

Для каждой пары (роль, пользователь ИБ, пароль) — COM-connect с Usr/Pwd и
пробный запрос «ВЫБРАТЬ ПЕРВЫЕ 1 * ИЗ <объект>». Результат per объект:
ok (доступ есть; rows=прочитано), denied (COM отказ по правам), error
(прочее: нет объекта, синтаксис...), auth_error (неверный логин/пароль).

Пользователи ИБ с нужными ролями создаются ЗАРАНЕЕ (агент может через
конфигуратор; инструмент намеренно не создаёт пользователей — мутация в ИБ).
Пароли НЕ включаются в отчёт. COM не прерываем — run_with_timeout (М-5
STA-поток, №8). connect_fn — инъекция для тестов (без Windows).
"""
from __future__ import annotations

DENIED_MARKERS = ("недостаточ", "права доступа", "доступ запрещ",
                  "insufficient", "permission")


class RightAuditError(RuntimeError):
    pass


def _classify_error(text: str) -> str:
    low = (text or "").lower()
    if "аутентифик" in low or "пароль" in low or "логин" in low or \
            "authentication" in low or "user not found" in low:
        return "auth_error"
    if any(m in low for m in DENIED_MARKERS):
        return "denied"
    return "error"


def audit(base: str, pairs: list, objects: list,
          timeout_s: float = 30.0, connect_fn=None) -> dict:
    """Матрица доступов: {roles: [{role, user, objects: {obj: вердикт}}],
    summary: {ok/denied/error/auth_error counts}}. Пароли не попадают в ответ."""
    if not pairs or not isinstance(pairs, list):
        raise RightAuditError("pairs обязателен: [{role, user, password}]")
    if not objects or not isinstance(objects, list):
        raise RightAuditError("objects обязателен: имена объектов 1С "
                              "(«Справочник.Валюты»)")
    if connect_fn is None:
        from eventlog_cfg import _connect as connect_fn  # noqa: E402

    roles = []
    counts = {"ok": 0, "denied": 0, "error": 0, "auth_error": 0}
    for pair in pairs:
        role = str(pair.get("role") or pair.get("user") or "?")
        user = str(pair.get("user") or "")
        row = {"role": role, "user": user, "objects": {}}
        for obj in objects:
            verdict = {"verdict": "error", "detail": ""}

            def work():
                conn = connect_fn(base, user=user,
                                  password=str(pair.get("password") or ""))
                q = conn.NewObject("Запрос")
                q.Текст = "ВЫБРАТЬ ПЕРВЫЕ 1 * ИЗ %s" % obj
                res = q.Выполнить()
                sel = res.Выбрать()
                return sel.Количество()

            try:
                from eventlog_cfg import run_with_timeout
                n = run_with_timeout(work, timeout_s,
                                     "right_audit %s/%s" % (user, obj))
                verdict = {"verdict": "ok", "rows": int(n)}
                counts["ok"] += 1
            except Exception as e:
                text = str(e)[:300]
                kind = _classify_error(text)
                verdict = {"verdict": kind, "detail": text[:200]}
                counts[kind] = counts.get(kind, 0) + 1
            row["objects"][obj] = verdict
        roles.append(row)
    return {"base": base, "objects": list(objects), "roles": roles,
            "summary": {"roles": len(pairs), "probes": len(pairs) * len(objects),
                        **counts}}
