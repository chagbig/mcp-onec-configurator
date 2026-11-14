# -*- coding: utf-8 -*-
r"""spec_check — сверка утверждений ТЗ/спеки с метаданными живой конфигурации
(IDEAS #130, стадия work).

Деление ролей: LLM-агент разбирает текст ТЗ на ПРОВЕРЯЕМЫЕ УТВЕРЖДЕНИЯ (это его
работа, НЕ инструмента); инструмент принимает уже структурированные утверждения
и проверяет их ПРОТИВ КОНФИГУРАЦИИ.

Формат утверждения (dict в списке claims, JSON):
  {"text": "Текст утверждения из ТЗ",
   "objects": ["Справочник.Договоры", "Документ.ЗаказКлиента"],
   "queries": ["ВЫБРАТЬ ..."]}   # опционально; валидируются через query_validate
                                 # при заданном base (mode="check")

Что проверяется на КАЖДЫЙ УНИКАЛЬНЫЙ объект (реестр собирается по всем claims:
один объект в двух утверждениях проверяется ОДИН раз):
  - exists:  md_object — объект есть в живой конфигураторе (uuid, canonical
             Вид.Имя, класс);
  - kind:    вид из ссылки корректен; незнакомый вид («Чудо.Х») — НЕ крах, а
             per-object вердикт unknown_kind;
  - similar: md_find по ключевому фрагменту имени (последний сегмент; короткие
             имена <4 символов — пропуск) — «похожий механизм уже есть»
             (до 5 кандидатов; fake-мост ищет и по синониму);
  - impact:  где объект уже упоминается в коде — md_search_code(«Менеджеры.Имя»)
             по свежему cf (для ОбщийМодуль — по имени модуля). ВАЖНО: снимает
             cf основной агент (cfg_archive_cf), инструмент cf_path только
             ПРИНИМАЕТ. Без cf — impact="" и note «снимите cf для карты влияния»;
  - queries: query_validate(query, base, mode="check") при заданном base
             (иначе пропуск с note).

Вердикт per-claim: ok | missing_objects (перечислены отсутствующие, uuid="") |
warnings (similar-кандидаты, impact-задетые объекты не из claim'а,
unknown_kind, проваленные запросы). Итог:
  {verdicts: [...], objects_checked: N, summary: "..."}

CLI:
  python spec_check.py --spec claims.json [--session N] [--container N] \
      [--cf путь.cf] [--base путь-иб]

Коды возврата: 0 — все утверждения ok; 1 — есть missing_objects;
2 — ошибка выполнения (файл спеки, мост недоступен и т.п.).

Столкновения и решения:
- мост 3.0.66 md_find ищет ПО ИМЕНИ объекта (см. bridge/mcp_bridge.js OPS.md_find);
  fake-мост дополнительно матчит синоним — тесты фиксируют ожидаемую семантику;
- провал query_validate ({ok:false, error}) — это ВЕРДИКТ валидации, попадает в
  warnings claim'а и в queries с текстом ошибки 1С.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_snegopat.bridge import BridgeError, NoSessionError  # noqa: E402

# известные виды метаданных (первый сегмент ссылки «Вид.Имя»)
KINDS = {
    "Справочник", "Документ", "Обработка", "Отчет", "Перечисление",
    "Константа", "ОбщийМодуль", "ОбщаяФорма", "ОбщаяКоманда", "ОбщийМакет",
    "РегистрСведений", "РегистрНакопления", "РегистрБухгалтерии",
    "РегистрРасчета", "ПланВидовХарактеристик", "ПланСчетов",
    "ПланВидовРасчета", "ПланОбмена", "БизнесПроцесс", "Задача",
    "Роль", "ОпределяемыйТип", "Подсистема", "КритерийОтбора",
    "ЖурналДокументов", "ФункциональнаяОпция",
}

# вид -> имя менеджера в коде («Справочники.Имя»); ОбщийМодуль — голое имя модуля
MANAGER = {
    "Справочник": "Справочники", "Документ": "Документы",
    "Обработка": "Обработки", "Отчет": "Отчеты",
    "Перечисление": "Перечисления", "Константа": "Константы",
    "РегистрСведений": "РегистрыСведений", "РегистрНакопления": "РегистрыНакопления",
    "РегистрБухгалтерии": "РегистрыБухгалтерии", "РегистрРасчета": "РегистрыРасчета",
    "ПланВидовХарактеристик": "ПланыВидовХарактеристик",
    "ПланСчетов": "ПланыСчетов", "ПланВидовРасчета": "ПланыВидовРасчета",
    "ПланОбмена": "ПланыОбмена", "БизнесПроцесс": "БизнесПроцессы",
    "Задача": "Задачи", "ОбщийМодуль": None,
}

SIMILAR_MIN_LEN = 4   # короче — similar-поиск пропускается (шум)
SIMILAR_LIMIT = 5     # до 5 похожих кандидатов
IMPACT_LIMIT = 20


def _split_ref(ref: str) -> tuple[str, str, str]:
    """«Справочник.Договоры» -> (Справочник, Договоры, Договоры);
    последний сегмент — ключ для similar-поиска."""
    parts = [p for p in str(ref).split(".") if p]
    kind = parts[0] if parts else ""
    name = parts[1] if len(parts) > 1 else ""
    last = parts[-1] if parts else ""
    return kind, name, last


def _impact_query(ref: str) -> str:
    """Литерал менеджера для поиска в коде: «Справочники.Договоры»;
    для ОбщийМодуль — голое имя модуля. Пусто — impact неприменим."""
    kind, name, _ = _split_ref(ref)
    if len([p for p in ref.split(".") if p]) != 2:
        return ""  # не верхнеуровневый объект — менеджерного литерала нет
    if kind == "ОбщийМодуль":
        return name
    mgr = MANAGER.get(kind)
    return "%s.%s" % (mgr, name) if (mgr and name) else ""


def _check_object(tk, session: int | None, container: int | None,
                  ref: str, cf_path: str) -> dict:
    """Проверка одного уникального объекта реестра."""
    res: dict = {"ref": ref, "status": "", "uuid": "", "cls": "",
                 "similar": [], "impact": [], "notes": []}
    kind, _name, last = _split_ref(ref)
    if kind not in KINDS:
        res["status"] = "unknown_kind"
        res["notes"].append("вид «%s» неизвестен — проверка пропущена" % kind)
        return res

    # exists
    try:
        obj = tk.md_object(path=ref, session=session, container=container)
    except RuntimeError:
        res["status"] = "missing"
        res["notes"].append("объект не найден в живой конфигурации")
        obj = None
    if obj is not None:
        res["status"] = "exists"
        res["uuid"] = str(obj.get("id") or "")
        res["cls"] = str(obj.get("cls") or kind)
        syn = str(obj.get("synonymRu") or "")
        if syn:
            res["synonym"] = syn
        if obj.get("cls") and obj["cls"] != kind:
            res["notes"].append("класс объекта «%s» не совпал с видом ссылки «%s»"
                                % (obj["cls"], kind))

    # similar: «похожий механизм уже есть» (мост ищет и по синониму)
    if len(last) < SIMILAR_MIN_LEN:
        res["notes"].append("имя короткое (<%d) — similar-поиск пропущен" % SIMILAR_MIN_LEN)
    else:
        try:
            f = tk.md_find(mask=last, session=session, container=container,
                           limit=IMPACT_LIMIT)
            cands = [m for m in (f.get("matches") or []) if m.get("path") != ref]
            res["similar"] = [{"path": m.get("path"), "name": m.get("name"),
                               "cls": m.get("cls")} for m in cands[:SIMILAR_LIMIT]]
        except RuntimeError as e:
            res["notes"].append("similar-поиск не выполнен: %s" % e)

    # impact: где объект уже упоминается в коде (по сохранённому cf)
    if not cf_path:
        res["notes"].append("cf не задан — карта влияния не строилась "
                            "(снимите cf через cfg_archive_cf для md_search_code)")
    elif res["status"] != "exists":
        res["notes"].append("объект отсутствует — карта влияния не строилась")
    else:
        q = _impact_query(ref)
        if not q:
            res["notes"].append("impact: менеджер вида «%s» не известен — пропущено" % kind)
        else:
            try:
                sr = tk.md_search_code(query=q, cf_path=cf_path, limit=IMPACT_LIMIT)
                res["impact"] = sorted({str(m.get("object"))
                                        for m in (sr.get("matches") or [])
                                        if m.get("object")})
            except RuntimeError as e:
                res["notes"].append("impact не выполнен: %s" % e)
    return res


def _check_queries(tk, claim: dict, base: str) -> tuple[list, list]:
    """Валидация запросов claim'а. Возвращает (результаты, notes)."""
    out, notes = [], []
    queries = [str(q) for q in (claim.get("queries") or []) if str(q).strip()]
    if not queries:
        return out, notes
    if not base:
        notes.append("base не задан — запросов не проверено: %d" % len(queries))
        return out, notes
    for q in queries:
        try:
            qr = tk.query_validate(query=q, base=base, mode="check")
        except RuntimeError as e:
            out.append({"query": q[:120], "ok": False, "error": str(e)})
            continue
        ok = bool(qr.get("ok"))
        entry: dict = {"query": q[:120], "ok": ok}
        if not ok:
            entry["error"] = str(qr.get("error") or "")
        out.append(entry)
    return out, notes


def verify_claims(tk, session: int | None, claims: list,
                  container: int | None = None, base: str = "",
                  cf: str = "") -> dict:
    """Сверка утверждений спеки с живой конфигурацией. claims — список dict
    (text, objects, queries). cf — свежий архив от cfg_archive_cf для карты
    влияния (снимает его основной агент — инструмент только принимает путь).
    Мост недоступен (session None и живых нет) — честный отказ (NoSessionError)."""
    if session is None and not tk.bridge.live_sessions():
        raise NoSessionError(
            "мост недоступен: живых сессий нет — откройте конфигуратор "
            "(мост) или укажите --session N")

    norm: list[dict] = []
    for raw in (claims or []):
        if isinstance(raw, dict):
            c = raw
        else:
            c = {"text": str(raw)}
        refs = list(dict.fromkeys(str(o) for o in (c.get("objects") or []) if str(o).strip()))
        norm.append({"text": str(c.get("text") or ""), "objects": refs,
                     "queries": [str(q) for q in (c.get("queries") or []) if str(q).strip()]})

    # уникальный реестр объектов по всем claims (порядок первого упоминания)
    registry: list[str] = []
    for c in norm:
        for r in c["objects"]:
            if r not in registry:
                registry.append(r)

    checked = {r: _check_object(tk, session, container, r, cf) for r in registry}

    verdicts: list[dict] = []
    n_ok = n_warn = n_miss = 0
    for i, c in enumerate(norm):
        objs = [checked[r] for r in c["objects"]]
        claim_set = set(c["objects"])
        missing = [o["ref"] for o in objs if o["status"] == "missing"]
        warnings: list[str] = []
        for o in objs:
            if o["status"] == "unknown_kind":
                warnings.append("unknown_kind: %s" % o["ref"])
            for s in o["similar"]:
                if s.get("path") and s["path"] not in claim_set:
                    warnings.append("similar: «%s» — похожий объект уже есть" % s["path"])
            for imp in o["impact"]:
                if imp not in claim_set:
                    warnings.append("impact: «%s» уже упоминается в коде (проверяли %s)"
                                    % (imp, o["ref"]))
        queries_res, qnotes = _check_queries(tk, c, base)
        for q in queries_res:
            if not q.get("ok"):
                warnings.append("query: «%s» — %s" % (q["query"], q.get("error") or "ошибка"))

        if missing:
            verdict = "missing_objects"
            n_miss += 1
        elif warnings:
            verdict = "warnings"
            n_warn += 1
        else:
            verdict = "ok"
            n_ok += 1
        v: dict = {"index": i, "text": c["text"], "verdict": verdict,
                   "objects": objs, "missing": missing, "warnings": warnings,
                   "queries": queries_res, "notes": list(qnotes)}
        if not c["objects"] and not c["queries"]:
            v["notes"].append("в утверждении нет объектов/запросов — проверять нечего")
        verdicts.append(v)

    return {"verdicts": verdicts,
            "objects_checked": len(registry),
            "objects": [checked[r] for r in registry],
            "summary": ("утверждений: %d; объектов уникальных: %d; ok: %d, "
                        "warnings: %d, missing_objects: %d"
                        % (len(norm), len(registry), n_ok, n_warn, n_miss))}


def _make_toolkit():
    """Отдельная функция для тестов (подмена на fake-мост)."""
    from mcp_snegopat.config import load_config
    from mcp_snegopat.server import Toolkit
    return Toolkit(load_config())


def load_spec(path: str) -> list:
    """Спека: список claims или {"claims": [...]}. Иначе — ValueError."""
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if isinstance(data, dict) and isinstance(data.get("claims"), list):
        return data["claims"]
    if isinstance(data, list):
        return data
    raise ValueError("спека: ожидается список claims или {\"claims\": [...]}")


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description='Сверка утверждений ТЗ/спеки с метаданными живой конфигурации')
    ap.add_argument('--spec', required=True, help='json-файл со списком claims')
    ap.add_argument('--session', type=int, default=None, help='pid конфигуратора')
    ap.add_argument('--container', type=int, default=None, help='индекс контейнера')
    ap.add_argument('--cf', default='', help='свежий cf (от cfg_archive_cf) для карты влияния')
    ap.add_argument('--base', default='', help='путь файловой ИБ для query_validate')
    a = ap.parse_args(argv)
    try:
        claims = load_spec(a.spec)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print('spec_check: спека не читается: %s' % e, file=sys.stderr)
        return 2
    try:
        tk = _make_toolkit()
        res = verify_claims(tk, a.session, claims, container=a.container,
                            base=a.base, cf=a.cf)
    except Exception as e:  # noqa: BLE001 — CLI: любая ошибка выполнения = rc 2
        print('spec_check: %s' % e, file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if any(v["verdict"] == "missing_objects" for v in res["verdicts"]):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
