# -*- coding: utf-8 -*-
r"""cf_arbiter — арбитр diff: выборочная верификация кандидатов платформенным
объединением /MergeCfg -Settings (IDEAS №12).

Идея. cf_recdiff/cf_diff дают список кандидатов «чем отличаются cf1 и cf2»,
но cf-хеши НЕ стабильны между сохранениями (ФАКТ П14-4 в cf_diff: 32 ложных
changed на паре с 1 реальным изменением) — кандидата надо подтвердить
авторитетом платформы. Арбитр делает выборочную приёмку: временная база из
cf1 (mockup_init), ОДНА пакетная команда /MergeCfg cf2 -Settings <settings.xml>
(переносится РОВНО перечисленный список объектов), отдельный /DumpCfg результата
в result.cf, затем сверка записей result.cf против cf2 по cf_inspect:
для каждого объекта — (uuid, суффикс, sha1 payload) → вердикт
identical | differs | missing.

Формат settings.xml — по проверенному живьём образцу (факт П4,
docs/stage-12-mcp-line-integration.md §9, 25.10.2026: /MergeCfg -Settings 13 с,
с UpdateDBCfg 34 с против ~90 с LoadCfg; вендор байт-в-байт цел; эталонный файл
out/p4-merge-settings.xml + генератор kit merge_settings.py):

  * атрибут version СТРОЧНЫМИ: version="1.2" (с «Version» платформа отказывает);
  * шапки Main/SecondConfiguration НЕ писать; параметры — только внутри
    <Parameters> (ConfigurationsRelation, AllowMainConfigurationObjectDeletion,
    CopyObjectsMode — порядок строго из kit);
  * первая запись <Objects> — <Configuration><MergeRule>DoNotMerge</MergeRule>
    (без неё всё НЕперечисленное берётся из второй конфигурации — правка
    вендора затрётся);
  * перечисленные объекты — <Object fullNameInSecondConfiguration="Вид.Имя">
    с правилом GetFromSecondConfiguration (новый объект и перенос целиком).

Формат проверен живым прогоном П4 на ОДНОМ объекте (принят, res=0). Комбинация
«список из нескольких объектов» правилами kit поддерживается, но живой прогон
арбитра на реальной паре cf ОБЯЗАТЕЛЕН перед боевым применением.

Вердикты и их честность:
  identical — сильный позитив: записи объекта совпали (суффиксы + sha1 payload);
  differs   — записи различаются. Ловушка: cf-контейнер может нормализоваться
              при сохранении (П14-4), поэтому differs НЕ означает, что объединение
              сломало объект — спорные перепроверяются каноном (cf_diff XML);
  missing   — объект не найден/не разрешён по имени в result.cf, либо его нет
              в cf2 (сверять не с чем). Имя разрешается по сериализации
              «{1,0,<uuid>},"Имя",» (cf_dump_json); cf не хранит ВИД объекта —
              открытая проблема резолвера, поэтому сверка идёт по голому имени
              (последняя часть «Вид.Имя»); неоднозначное имя — отказ.

Ловушки проекта, соблюдённые в конвейере:
  * одна команда конфигуратора = один вызов процесса (склейка пакетных команд
    висит — этап 12/П2): MergeCfg и DumpCfg — два отдельных вызова;
  * пакетный вызов — только на базу, НЕ занятую конфигуратором («1Cv8.cdn»);
  * /DisableStartupDialogs в каждом вызове (иначе ошибка поднимает модалку
    навсегда); процесс по таймауту снимается (Popen + kill, ревью этапа 12 №6).

parse_recdiff_candidates: кандидаты из отчёта cf_recdiff (JSON). cf_recdiff v1.3
хранит имена записей (uuid) и голые имена объектов из сериализаций — БЕЗ вида
(«Справочник.Х») и БЕЗ признака расширения: cfe-специфику в отчёте неотличима
(расширение живёт в cfe-контейнере, а не в основном cf — в паре основных cf его
не должно быть; явного фильтра нет, честно упоминается). Голые имена напрямую в
настройки MergeCfg не годятся (платформа требует «Вид.Имя», cf_diff.run_hybrid)
— резолвер вида остаётся открытой проблемой; CLI берёт из recdiff-отчёта только
имена, у которых вид уже указан.

CLI:
  python cf_arbiter.py --cf1 <вендор> --cf2 <доработка> --objects "Документ.X,Справочник.Y" \
      [--work <каталог>] [--keep]
  python cf_arbiter.py --cf1 ... --cf2 ... --recdiff <recdiff.json> --max-n 10 [--work] [--keep]

Коды возврата: 0 — все вердикты identical; 1 — есть differs/missing;
2 — ошибка (аргументы, платформа, отказ).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

PLATFORM = r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe"  # как в mockup_init/cf_diff
SETTINGS_NS = "http://v8.1c.ru/8.3/config/merge/settings"
PLATFORM_VERSION = "8.3.11"  # как в проверенном out/p4-merge-settings.xml

# Виды метаданных для полного имени «Вид.Имя» (защита от ловушки cf_diff №12:
# подчинённые/bare-имена — гарантированный ложный FAIL постусловия платформой)
KNOWN_KINDS = frozenset((
    "Константа", "Справочник", "Документ", "ПланВидовРасчёта", "РегистрСведений",
    "РегистрНакопления", "РегистрБухгалтерии", "РегистрРасчёта", "БизнесПроцесс",
    "Задача", "ПланОбмена", "ПланСчетов", "ПланВидовХарактеристик", "Обработка",
    "Отчёт", "ОбщаяФорма", "ОбщаяКоманда", "ОбщийМакет", "ОбщийМодуль",
    "ПараметрЗапуска", "Роль", "Сеанс", "ВнешниеИсточникиДанных", "XDTOПакет",
    "ЖурналРегистрации", "СоставСхемыДанных",
))

_SERVICE_NAMES = frozenset(("root", "version", "versions", "configinfo", "deleted"))


class ArbiterError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 1. Настройки объединения
# --------------------------------------------------------------------------

def build_merge_settings(objects: list[str]) -> str:
    """XML настроек /MergeCfg -Settings для списка полных имён («Документ.X»).

    Строго по проверенному образцу (out/p4-merge-settings.xml, факт П4):
    version lowercase, <Parameters> с тремя параметрами, <Objects> с первой
    записью <Configuration> DoNotMerge и по одной <Object
    fullNameInSecondConfiguration="..."> GetFromSecondConfiguration на объект.
    Пустой список и дубли — отказ. Чистая функция, диск не трогает.
    """
    objs = [str(o).strip() for o in (objects or [])]
    objs = [o for o in objs if o]
    if not objs:
        raise ArbiterError("список объектов пуст — объединение без списка запрещено "
                           "(всё перенеслось бы из второй конфигурации)")
    dupes = sorted({o for o in objs if objs.count(o) > 1})
    if dupes:
        raise ArbiterError("дубли в списке объектов: %s" % ", ".join(dupes))
    from xml.sax.saxutils import quoteattr

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Settings xmlns="%s" version="1.2" platformVersion="%s">' % (SETTINGS_NS, PLATFORM_VERSION),
        "\t<Parameters>",
        "\t\t<ConfigurationsRelation>ConfigurationsNotRelated</ConfigurationsRelation>",
        "\t\t<AllowMainConfigurationObjectDeletion>false</AllowMainConfigurationObjectDeletion>",
        "\t\t<CopyObjectsMode>false</CopyObjectsMode>",
        "\t</Parameters>",
        "\t<Objects>",
        "\t\t<Configuration>",
        "\t\t\t<MergeRule>DoNotMerge</MergeRule>",
        "\t\t</Configuration>",
    ]
    for obj in objs:
        lines += [
            "\t\t<Object fullNameInSecondConfiguration=%s>" % quoteattr(obj),
            "\t\t\t<MergeRule>GetFromSecondConfiguration</MergeRule>",
            "\t\t</Object>",
        ]
    lines += ["\t</Objects>", "</Settings>"]
    return "\n".join(lines) + "\n"


def check_objects(objects: list[str]) -> list[str]:
    """Валидация списка полных имён (Ловушка cf_diff №12: подчинённые имена →
    гарантированный ложный FAIL платформой; bare-имена тоже не принимаются)."""
    if not objects:
        raise ArbiterError("список объектов пуст")
    for obj in objects:
        parts = obj.split(".")
        if len(parts) != 2 or not all(parts):
            raise ArbiterError(
                "«%s»: ожидается ПОЛНОЕ имя «Вид.Имя» — подчинённые (три части) и "
                "голые имена MergeCfg не принимает" % obj)
        if parts[0] not in KNOWN_KINDS:
            raise ArbiterError(
                "«%s»: неизвестный вид «%s» (не в списке KNOWN_KINDS — опечатка или "
                "подчинённый объект)" % (obj, parts[0]))
    return list(objects)


# --------------------------------------------------------------------------
# 2. Кандидаты из отчёта cf_recdiff
# --------------------------------------------------------------------------

def parse_recdiff_candidates(recdot_path: str, max_n: int = 10) -> list[str]:
    """До max_n ИМЁН объектов-кандидатов из JSON-отчёта cf_recdiff (v1.3).

    Формат отчёта (cf_recdiff.scan): {"new": [имена записей], "deleted": [...],
    "changed": [...], "named": [{"record", "name"}], ...}; имена записей —
    uuid или uuid.N (дубли (uuid, суффикс) ЛЕГАЛЬНЫ — разворачиваются в одно
    имя объекта). Имя берётся из named по записи или по её базовому uuid;
    записи без имени в сериализациях пропускаются.

    Ограничения (честно): имена БЕЗ вида («Номенклатура», не «Справочник.Номенклатура»)
    — cf не хранит вид, резолвер — открытая проблема (cf_diff.run_hybrid);
    cfe-специфика в отчёте не помечена (расширение живёт в cfe-контейнере, в паре
    основных cf его быть не должно — фильтра нет).
    """
    data = json.loads(Path(recdot_path).read_text(encoding="utf-8"))
    named = {}
    for e in data.get("named") or []:
        rec, nm = e.get("record"), e.get("name")
        if rec and nm and nm not in _SERVICE_NAMES:
            named.setdefault(str(rec), nm)
            named.setdefault(str(rec).split(".")[0].lower(), nm)
    out: list[str] = []
    seen: set[str] = set()
    skipped = 0
    for group in ("new", "deleted", "changed"):
        for rec in data.get(group) or []:
            nm = named.get(str(rec)) or named.get(str(rec).split(".")[0].lower())
            if not nm or nm in seen:
                if not nm:
                    skipped += 1
                continue
            seen.add(nm)
            out.append(nm)
            if len(out) >= max_n:
                return out
    if skipped:
        # не теряем молча: имя не разрешено — кандидат без вида бесполезен для MergeCfg
        print("рекдифф: %d записей без разрешённого имени пропущено" % skipped,
              file=sys.stderr)
    return out


# --------------------------------------------------------------------------
# 3. Верификация по cf_inspect
# --------------------------------------------------------------------------

def _signatures(cf_path: str) -> tuple[dict, dict, set]:
    """(uuid → [(суффикс, sha1)], голое имя → uuid, неоднозначные имена).

    По cf_dump_json.dump(use_hash=True) — потоковое чтение через cf_inspect,
    платформа не нужна. Неоднозначное имя (два uuid с одной сериализацией) —
    сигнал мусора/рекреации: арбитр по нему отказывается сверять.
    """
    from cf_dump_json import dump  # noqa: E402

    res = dump(cf_path, use_hash=True)
    by_uuid: dict[str, list] = {}
    by_name: dict[str, str] = {}
    ambiguous: set[str] = set()
    for o in res["objects"]:
        uuid = o["uuid"].lower()
        entries = sorted((e["suffix"], e.get("hash")) for e in o["entries"])
        by_uuid[uuid] = entries
        nm = o.get("name")
        if nm:
            if nm in by_name and by_name[nm] != uuid:
                ambiguous.add(nm)
            else:
                by_name[nm] = uuid
    return by_uuid, by_name, ambiguous


def verify_objects(result_cf: str, cf2: str, objects: list[str]) -> list[dict]:
    """Per-object сверка result.cf против cf2 (эталон — вторая конфигурация):
    совпадение списка (суффикс, sha1 payload) записей объекта."""
    r_uuid, r_name, r_amb = _signatures(result_cf)
    c_uuid, c_name, _ = _signatures(cf2)
    verdicts = []
    for obj in objects:
        bare = obj.split(".")[-1]
        if bare in r_amb:
            raise ArbiterError("«%s»: имя неоднозначно в result.cf (несколько uuid с "
                               "одной сериализацией) — сверка вслепую запрещена" % obj)
        r_u = r_name.get(bare)
        if not r_u or r_u not in r_uuid:
            verdicts.append({"object": obj, "verdict": "missing",
                             "detail": "объект не найден/не разрешён по имени в result.cf"})
            continue
        c_u = c_name.get(bare)
        if not c_u or c_u not in c_uuid:
            verdicts.append({"object": obj, "verdict": "missing",
                             "detail": "объекта нет в cf2 — сверять не с чем"})
            continue
        r_set = {s for s, _ in r_uuid[r_u]}
        c_set = {s for s, _ in c_uuid[c_u]}
        if r_uuid[r_u] == c_uuid[c_u]:
            verdicts.append({"object": obj, "verdict": "identical", "detail": ""})
        else:
            only_r = ", ".join(sorted(r_set - c_set)) or "-"
            only_c = ", ".join(sorted(c_set - r_set)) or "-"
            hash_diff = [s for s, h in r_uuid[r_u]
                         if (s, h) not in c_uuid[c_u]]
            verdicts.append({
                "object": obj, "verdict": "differs",
                "detail": "записи result: [%s] cf2: [%s] (суффиксы только в result: %s, "
                          "только в cf2: %s)" % (", ".join(sorted(r_set)) or "-",
                                                 ", ".join(sorted(c_set)) or "-",
                                                 only_r, only_c)
                          + ("; hash отличается: " + ", ".join(hash_diff) if hash_diff else ""),
            })
    return verdicts


# --------------------------------------------------------------------------
# 4. Конвейер (шаги b/c/d — инъекция для тестов без платформы)
# --------------------------------------------------------------------------

def _designer(args: list[str], work: str, name: str, timeout: float = 7200) -> None:
    """ОДИН вызов DESIGNER с /DisableStartupDialogs; процесс по таймауту снимается
    (ревью этапа 12 №6: subprocess.call(timeout) 1cv8 не убивает)."""
    work_p = Path(work)
    work_p.mkdir(parents=True, exist_ok=True)
    log = work_p / ("arbiter-%s.log" % name)
    res = work_p / ("arbiter-%s.res" % name)
    res.unlink(missing_ok=True)
    cmd = [PLATFORM, "DESIGNER", "/DisableStartupDialogs"] + args \
        + ["/Out", str(log), "/DumpResult", str(res)]
    proc = subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise ArbiterError("DESIGNER %s: таймаут %d с — процесс снят" % (name, timeout))
    if rc != 0:
        raise ArbiterError("DESIGNER %s: rc=%d (журнал: %s)" % (name, rc, log))
    out = res.read_text(encoding="utf-8-sig", errors="replace").strip() if res.is_file() else ""
    if out.lstrip("﻿") != "0":
        raise ArbiterError("DESIGNER %s: DumpResult=%r (журнал: %s)" % (name, out, log))


def _default_init_base(cf1: str, base: str, work: str) -> None:
    """Временная база из cf1: mockup_init --from (LoadCfg + UpdateDBCfg последовательно
    — каждая команда отдельным вызовом внутри mockup_init)."""
    r = subprocess.run([sys.executable, str(TOOLS / "mockup_init.py"),
                        "--base", base, "--from", cf1, "--reset"],
                       capture_output=True, text=True, timeout=7200)
    if r.returncode != 0:
        raise ArbiterError("mockup_init: rc=%d: %s | %s"
                           % (r.returncode, r.stdout[-300:], r.stderr[-300:]))


def _default_merge_cfg(base: str, cf2: str, settings: str, work: str) -> None:
    # ОДНА команда (склейка пакетных команд висит — этап 12/П2)
    _designer(["/F", base, "/MergeCfg", cf2, "-Settings", settings], work, "merge")


def _default_dump_cfg(base: str, result_cf: str, work: str) -> None:
    # Отдельный вызов: одна команда конфигуратора = один процесс
    _designer(["/F", base, "/DumpCfg", result_cf], work, "dump")


def arbiter_run(cf1: str, cf2: str, objects: list[str], work: str = "",
                keep: bool = False, *,
                init_base=None, merge_cfg=None, dump_cfg=None, verify=None) -> dict:
    """Полный цикл верификации кандидатов (см. докстринг модуля).

    Шаги b/c/d (база из cf1, MergeCfg, DumpCfg) и сверка — инъекцией: в тестах
    подставляются фейки (образец — CloseSequence в test_enterprise_smoke), реальный
    1cv8 в тестах не запускается. Временная база удаляется (keep=False), work с
    settings.xml и result.cf сохраняется. Авария — cleanup базы всё равно идёт.
    """
    t0 = time.time()
    cf1p, cf2p = Path(cf1), Path(cf2)
    for p in (cf1p, cf2p):
        if not p.is_file():
            raise ArbiterError("cf не найден: %s" % p)
    objects = check_objects(objects)

    work_dir = Path(work) if work else Path(tempfile.mkdtemp(prefix="cf-arbiter-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    settings_file = work_dir / "settings.xml"
    settings_file.write_text(build_merge_settings(objects), encoding="utf-8", newline="\n")

    base = work_dir / "base-merge"
    result_cf = work_dir / "result.cf"
    init_base = init_base or _default_init_base
    merge_cfg = merge_cfg or _default_merge_cfg
    dump_cfg = dump_cfg or _default_dump_cfg
    verify = verify or verify_objects

    steps: list[str] = []
    try:
        init_base(str(cf1p), str(base), str(work_dir))
        steps.append("init-base")
        merge_cfg(str(base), str(cf2p), str(settings_file), str(work_dir))
        steps.append("merge-cfg")
        dump_cfg(str(base), str(result_cf), str(work_dir))
        steps.append("dump-cfg")
        if not result_cf.is_file():
            raise ArbiterError("result.cf не создан шагом dump-cfg")
        verdicts = verify(str(result_cf), str(cf2p), list(objects))
        steps.append("verify")
    finally:
        if not keep:
            try:
                shutil.rmtree(base)
            except OSError as e:
                print("база %s не удалена (занята?): %s" % (base, e), file=sys.stderr)
    return {
        "verdicts": verdicts,
        "settings_file": str(settings_file),
        "result_cf": str(result_cf),
        "work": str(work_dir),
        "elapsed_s": round(time.time() - t0, 1),
        "steps": steps,
        "kept_base": keep,
    }


# --------------------------------------------------------------------------
# 5. CLI
# --------------------------------------------------------------------------

def main(argv=None, run_fn=None) -> int:
    for stream in (sys.stdout, sys.stderr):   # образец blog_publish.py
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Арбитр diff: /MergeCfg -Settings как вердикт")
    ap.add_argument("--cf1", required=True, help="вендорский эталон (основа временной базы)")
    ap.add_argument("--cf2", required=True, help="доработанная конфигурация (эталон верификации)")
    ap.add_argument("--objects", default="",
                    help="полные имена через запятую: «Документ.X,Справочник.Y»")
    ap.add_argument("--recdiff", default="",
                    help="JSON-отчёт cf_recdiff: кандидаты (имена с видом, если разрешены)")
    ap.add_argument("--max-n", type=int, default=10, help="лимит кандидатов из --recdiff")
    ap.add_argument("--work", default="", help="рабочий каталог (по умолчанию — временный)")
    ap.add_argument("--keep", action="store_true", help="не удалять временную базу")
    a = ap.parse_args(argv)

    try:
        if a.objects:
            objects = [s.strip() for s in a.objects.split(",") if s.strip()]
        elif a.recdiff:
            candidates = parse_recdiff_candidates(a.recdiff, a.max_n)
            print("кандидаты из recdiff: %d:" % len(candidates))
            for c in candidates:
                print("  ", c)
            objects = [c for c in candidates if "." in c]
            if not objects:
                print("ОТКАЗ: имена из recdiff БЕЗ вида («Вид.Имя») — MergeCfg их не "
                      "принимает (резолвер вида — открытая проблема, cf_diff.run_hybrid); "
                      "укажите --objects вручную", file=sys.stderr)
                return 2
        else:
            print("ОТКАЗ: нужен --objects или --recdiff", file=sys.stderr)
            return 2
        rep = (run_fn or arbiter_run)(a.cf1, a.cf2, objects, work=a.work, keep=a.keep)
    except (ArbiterError, OSError, ValueError, json.JSONDecodeError) as e:
        print("ОТКАЗ: %s" % e, file=sys.stderr)
        return 2
    print("steps: %s | elapsed: %s с | work: %s"
          % (" -> ".join(rep["steps"]), rep["elapsed_s"], rep["work"]))
    bad = 0
    for v in rep["verdicts"]:
        mark = {"identical": "ИДЕНТИЧНО", "differs": "РАЗЛИЧИЕ", "missing": "ОТСУТСТВУЕТ"}[v["verdict"]]
        print("%-10s %s%s" % (mark, v["object"], (" — " + v["detail"]) if v["detail"] else ""))
        if v["verdict"] != "identical":
            bad += 1
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
