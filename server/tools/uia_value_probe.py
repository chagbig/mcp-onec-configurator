# -*- coding: utf-8 -*-
"""UIA-проба ValuePattern полей диалога конфигуратора (гипотеза записи текста).

Известный факт (v1.14, docs/uia-phase0-recon.md): поля диалогов конфигуратора
экспонируются через UIA (uia_dialog_read живьём видел 191 контрол, 9 полей
Edit на главном окне), но ВВОД ТЕКСТА type_text'ом (UNICODE SendInput) 1С
ФИЛЬТРУЕТ. Гипотеза: поля поддерживают UIA ValuePattern -> SetValue сможет
вводить текст НАПРЯМУЮ (через COM-провайдер окна), минуя клавиатуру.
Этот инструмент проверяет гипотезу: сканирует контролы главного окна,
для каждого читает Name/ControlTypeName/AutomationId/ClassName и
доступность ValuePattern (+ текущее значение через паттерн).

Порядок (приём uia_dialog_read/uia_tree — из потока сервера UIA-дерево
НЕ читается, COM STA): EnumWindows по pid -> ГЛАВНОЕ окно (максимальная
площадь) -> ControlFromHandle -> рекурсивный обход (глубина <= MAX_DEPTH,
бюджет <= MAX_CONTROLS) -> в отчёт попадают контролы типов
Edit/ComboBox/Document И любые с доступным ValuePattern.

Вывод JSON: {pid, window_title, controls: [{name, type, automation_id,
class_name, value_available, value}], stats: {total, edits,
with_value_pattern}, truncated, note}; при --set добавляется
set_result {set, field, error}, при --dry-write — dry_write
{would_set, field, value?, error}.

БЕЗОПАСНОСТЬ: скан по умолчанию read-only; запись ТОЛЬКО при явном --set;
поля, чьё Name содержит «пароль»/«password» (в любом регистре), НЕ пишутся.

CLI: python tools/uia_value_probe.py --pid <pid> [--out probe.json]
     [--set "ИмяПоля=Текст"] [--dry-write]
rc=0 успех (JSON в stdout или --out), rc=2 — ошибка окружения/аргументов.

ЖИВОЙ ПРОГОН (на станции владельца, конфигуратор запущен; 1cv8.exe -> pid):
  python tools/uia_value_probe.py --pid <pid> --out probe.json
  python tools/uia_value_probe.py --pid <pid> --set "Имя базы=Demo" --dry-write
  python tools/uia_value_probe.py --pid <pid> --set "Имя базы=Demo"
Если set_result.set=true и значение видно в окне (uia_dialog_read --
value совпал) — гипотеза подтверждена.

Зависимость: uiautomation (pip install uiautomation) — Windows-only; импорт
ленивый, после гвардов, поэтому модуль импортируется и на Linux (и в CI).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# CLI-запуск (в т.ч. python -I): сосед window_shot должен быть импортируемым
sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Максимальная глубина обхода дерева UIA (уровней контролов от корня).
MAX_DEPTH = 12

#: Бюджет посещённых контролов на один скан (защита от гигантских деревьев).
MAX_CONTROLS = 600

#: Типы контролов (ControlTypeName) — «поля ввода» для счётчика edits и
#: безусловного включения в отчёт (контролы с ValuePattern попадают и без них).
FIELD_TYPES = frozenset({"EditControl", "ComboBoxControl", "DocumentControl"})

#: Основы имени поля, запрещающие запись (безопасность, без учёта регистра).
#: «парол» — основа: ловит ВСЕ падежи («пароль», «пароля», «пароли», в т.ч.
#: «Подтверждение пароля»), надмножество литерального «пароль» из спеки.
PASSWORD_MARKERS = ("парол", "password")

#: Пояснение в поле note отчёта (попадает в каждый JSON-вывод).
NOTE = ("read-only скан ValuePattern полей; запись только при явном --set "
        "(ValuePattern.SetValue, минуя клавиатуру — 1С фильтрует SendInput); "
        "поля с «пароль»/«password» в имени защищены от записи")


def require_windows() -> None:
    """Гвард платформы: не Windows -> RuntimeError с понятным текстом."""
    if sys.platform != "win32":
        raise RuntimeError(
            "uia_value_probe работает только на Windows: UI Automation "
            "(COM, пакет uiautomation) недоступен на платформе %r"
            % sys.platform)


def _load_uia():
    """Импорт uiautomation ВНУТРИ функции: нет пакета -> RuntimeError с
    «pip install uiautomation» (как гвард uia_menu_map в server.py).
    Ленивость позволяет импортировать модуль (и гонять тесты) без пакета."""
    try:
        import uiautomation as auto
    except ImportError:
        raise RuntimeError(
            "uia_value_probe: нет uiautomation (pip install uiautomation)")
    return auto


def parse_set_arg(spec: str) -> tuple[str, str]:
    """Разобрать аргумент --set «ИмяПоля=Текст» -> (имя, текст).

    ValueError на пустой строке, отсутствии «=» и пустом имени поля.
    Значение обрезается по краям; пустое значение допустимо (очистка поля).
    """
    if not spec or not spec.strip():
        raise ValueError(
            "--set: пустая строка, ожидается «ИмяПоля=Текст»")
    if "=" not in spec:
        raise ValueError(
            "--set: нет «=», ожидается «ИмяПоля=Текст» (получено %r)" % spec)
    name, _, text = spec.partition("=")  # первое «=»: значение может содержать «=»
    name = name.strip()
    if not name:
        raise ValueError(
            "--set: пустое имя поля (получено %r)" % spec)
    return name, text.strip()


def is_password_field(name: str) -> bool:
    """True, если имя поля содержит «пароль» или «password» (любой регистр).
    Такие поля НЕ пишутся никогда (безопасность), даже при явном --set."""
    low = (name or "").lower()
    return any(marker in low for marker in PASSWORD_MARKERS)


def summarize(controls: list) -> dict:
    """stats из списка контролов (чистая функция над отчётом):
    total — всего в отчёте; edits — типов из FIELD_TYPES;
    with_value_pattern — с value_available=true."""
    return {
        "total": len(controls),
        "edits": sum(1 for c in controls if c.get("type") in FIELD_TYPES),
        "with_value_pattern": sum(1 for c in controls if c.get("value_available")),
    }


def _name_matches(name: str, needle: str) -> bool:
    """Подстрока needle в имени name без учёта регистра (match --set)."""
    return needle.lower() in (name or "").lower()


def match_control(controls: list, needle: str) -> dict | None:
    """Первый контрол отчёта, чьё Name содержит подстроку needle
    (case-insensitive); None — совпадений нет."""
    for c in controls:
        if _name_matches(c.get("name", ""), needle):
            return c
    return None


def check_target(name: str | None, value_available: bool,
                 needle: str) -> tuple[bool, str]:
    """Гварды записи: (ok, текст ошибки). Единая логика для --set и
    --dry-write: поле не найдено -> отказ; имя похоже на парольное -> отказ
    (безопасность); ValuePattern недоступен -> отказ."""
    if not name:
        return False, "поле с «%s» не найдено" % needle
    if is_password_field(name):
        return False, ("поле «%s» похоже на парольное («пароль»/«password» "
                       "в имени) — запись запрещена" % name)
    if not value_available:
        return False, "у «%s» нет ValuePattern — SetValue невозможен" % name
    return True, ""


def dry_write_result(target: dict | None, needle: str, text: str) -> dict:
    """ЧТО было бы записано (чистая функция для --dry-write, без записи):
    {would_set, field, value?, error} по гвардам check_target."""
    name = target.get("name") if target else None
    value_available = bool(target and target.get("value_available"))
    ok, err = check_target(name, value_available, needle)
    res: dict = {"would_set": ok,
                 "field": name if name else needle,
                 "error": err}
    if ok:
        res["value"] = text
    return res


def _control_probe(ctrl, value_pattern_id) -> tuple[dict | None, object]:
    """{name, type, automation_id, class_name, value_available, value} одного
    контрола + его ValuePattern (для последующей записи). Каждый — даже
    исчезнувший — контрол даёт запись (недочитанное свойство пропускается).
    Возврат (None, паттерн), если контрол НЕ поля и БЕЗ ValuePattern
    (кнопки/статики в отчёт не попадают)."""
    info = {"name": "", "type": "", "automation_id": "", "class_name": "",
            "value_available": False, "value": ""}
    for key, getter in (
            ("type", lambda: str(ctrl.ControlTypeName)),
            ("name", lambda: str(ctrl.Name or "")),
            ("automation_id", lambda: str(ctrl.AutomationId or "")),
            ("class_name", lambda: str(ctrl.ClassName or ""))):
        try:
            info[key] = getter()
        except Exception:
            pass  # элемент исчез / свойство недоступно
    pat = None
    try:
        pat = ctrl.GetPattern(value_pattern_id)  # None, если паттерна нет
    except Exception:
        pat = None
    info["value_available"] = pat is not None
    if pat is not None:
        # Value у uiautomation читает UIA-свойство CurrentValue провайдера
        try:
            info["value"] = str(pat.Value or "")
        except Exception:
            try:
                info["value"] = str(pat.GetValue() or "")
            except Exception:
                info["value"] = ""
    if info["type"] in FIELD_TYPES or info["value_available"]:
        return info, pat
    return None, pat


def _scan_tree(root, value_pattern_id, max_depth: int,
               max_controls: int) -> tuple[list, list, bool]:
    """Рекурсивный обход (pre-order) от корня: (controls, patterns, truncated).
    controls[i] и patterns[i] (живой ValuePattern или None) выровнены по
    индексам — run_set сопоставляет отчёт и живые контролы. Бюджет исчерпан
    -> truncated=true (дальше не спускаемся)."""
    controls: list = []
    patterns: list = []
    budget = {"count": 0, "truncated": False}

    def visit(ctrl, depth: int) -> None:
        if budget["count"] >= max_controls:
            budget["truncated"] = True
            return
        budget["count"] += 1
        info, pat = _control_probe(ctrl, value_pattern_id)
        if info is not None:
            controls.append(info)
            patterns.append(pat)
        if depth >= max_depth:
            return  # сам контрол учтён, вниз не спускаемся
        try:
            child = ctrl.GetFirstChildControl()
        except Exception:
            child = None
        while child is not None:
            visit(child, depth + 1)
            if budget["count"] >= max_controls:
                return
            try:
                child = child.GetNextSiblingControl()
            except Exception:
                child = None

    visit(root, 0)
    return controls, patterns, budget["truncated"]


def probe_full(pid: int, max_depth: int = MAX_DEPTH,
               max_controls: int = MAX_CONTROLS) -> tuple[dict, list]:
    """Скан главного окна процесса pid: (отчёт, patterns).

    Отчёт — JSON-совместимый dict (build_report); patterns[i] — живой
    ValuePattern контрола controls[i] (или None) для последующего --set.
    Не Windows -> RuntimeError; нет uiautomation -> RuntimeError; нет
    видимых окон -> RuntimeError; UIA не открыл окно -> RuntimeError.
    """
    require_windows()
    auto = _load_uia()
    from window_shot import list_windows  # pywin32 — после гварда платформы
    wins = list_windows(pid)
    if not wins:
        raise RuntimeError(
            "у процесса %d нет видимых окон верхнего уровня" % pid)
    win = max(wins, key=lambda w: (w["rect"][2] - w["rect"][0])
                                  * (w["rect"][3] - w["rect"][1]))
    root = auto.ControlFromHandle(win["hwnd"])
    if root is None:
        raise RuntimeError(
            "UIA не открыл окно hwnd=%s (pid %d)" % (win["hwnd"], pid))
    controls, patterns, truncated = _scan_tree(
        root, auto.PatternId.ValuePattern, max_depth, max_controls)
    title = ""
    try:
        title = str(root.Name or "")
    except Exception:
        pass
    return build_report(pid, title or win["title"], controls, truncated), patterns


def build_report(pid: int, window_title: str, controls: list,
                 truncated: bool = False) -> dict:
    """JSON-отчёт скана (контракт CLI): {pid, window_title, controls,
    stats, truncated, note}. Чистая функция — тестируется без UIA."""
    return {"pid": pid,
            "window_title": window_title,
            "controls": controls,
            "stats": summarize(controls),
            "truncated": bool(truncated),
            "note": NOTE}


def run_set(controls: list, patterns: list, needle: str, text: str) -> dict:
    """Запись текста ValuePattern.SetValue в найденный по подстроке имени
    контрол (case-insensitive): {set: bool, field, error}. Гварды
    check_target общие с --dry-write (пароль-поля не пишутся никогда);
    ошибка SetValue честно попадает в error. ТОЛЬКО для явного --set."""
    matched = None
    for c, pat in zip(controls, patterns):
        if _name_matches(c.get("name", ""), needle):
            matched = (c, pat)
            break
    name = matched[0].get("name") if matched else None
    value_available = bool(matched and matched[0].get("value_available"))
    ok, err = check_target(name, value_available, needle)
    field = name if name else needle
    if not ok:
        return {"set": False, "field": field, "error": err}
    try:
        matched[1].SetValue(text)
    except Exception as e:
        return {"set": False, "field": field, "error": "SetValue: %s" % e}
    return {"set": True, "field": field, "error": ""}


def run_set_index(controls: list, patterns: list, index: int,
                  text: str) -> dict:
    """Запись SetValue по ИНДЕКСУ в списке контролов отчёта (v1.17, живая
    проба: поля конфигуратора часто БЕЗЫМЯННЫЕ — check_target с матчем по
    имени их отсекает; индекс берётся из предыдущего read-only скана).
    Гварды: индекс в границах; пароль-гвард по имени (is_password_field —
    именованные пароль-поля не обходятся стороной); ValuePattern доступен и
    живой паттерн получен при скане. Индекс за границы / без паттерна —
    честный отказ."""
    if not (0 <= index < len(controls)):
        return {"set": False, "field": "#%d" % index,
                "error": "индекс за границами (%d контролов)" % len(controls)}
    c, pat = controls[index], patterns[index]
    name = c.get("name") or "#%d" % index
    if is_password_field(c.get("name")):
        return {"set": False, "field": name,
                "error": ("поле «%s» похоже на парольное («пароль»/«password» "
                          "в имени) — запись запрещена" % c.get("name"))}
    if not c.get("value_available"):
        return {"set": False, "field": name,
                "error": "у «%s» нет ValuePattern — SetValue невозможен" % name}
    if pat is None:
        return {"set": False, "field": name,
                "error": "живой ValuePattern не получен при скане"}
    try:
        pat.SetValue(text)
    except Exception as e:
        return {"set": False, "field": name, "error": "SetValue: %s" % e}
    return {"set": True, "field": name, "error": ""}


def probe(pid: int, max_depth: int = MAX_DEPTH,
          max_controls: int = MAX_CONTROLS) -> dict:
    """Read-only скан (библиотечная обёртка probe_full без живых паттернов):
    {pid, window_title, controls, stats, truncated, note}. Запись текста —
    только через CLI --set / run_set."""
    return probe_full(pid, max_depth, max_controls)[0]


def main(argv: list | None = None) -> int:
    """CLI: --pid (обязателен), --out <json>, --set «Имя=Текст»,
    --dry-write; rc=0/2. Скан по умолчанию read-only."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description="UIA-проба ValuePattern полей диалога конфигуратора: "
                    "скан read-only по умолчанию; запись текста только "
                    "при явном --set (SetValue, минуя клавиатуру)")
    ap.add_argument("--pid", type=int, required=True,
                    help="pid процесса 1cv8 (конфигуратора)")
    ap.add_argument("--out", default="",
                    help="путь JSON-вывода (по умолчанию stdout)")
    ap.add_argument("--set", dest="set_spec", default="", metavar="ИМЯ=ТЕКСТ",
                    help="ЯВНАЯ запись: подстрока имени поля = значение "
                         "(поля с «пароль»/«password» не пишутся)")
    ap.add_argument("--index", type=int, default=-1, metavar="N",
                    help="запись SetValue по индексу контрола из read-only "
                         "скана — для БЕЗЫМЯННЫХ полей конфигуратора "
                         "(значение задаёт --set-text; пароль-поля по имени "
                         "не пишутся)")
    ap.add_argument("--set-text", dest="set_text_arg", default="",
                    metavar="ТЕКСТ",
                    help="значение для записи при --index (без «Имя=»)")
    ap.add_argument("--dry-write", action="store_true",
                    help="показать, ЧТО было бы записано (--set не выполняется)")
    args = ap.parse_args(argv)
    try:
        if args.dry_write and not args.set_spec:
            raise ValueError(
                "--dry-write требует --set «Имя=Текст»: нечего показывать")
        if args.index >= 0 and args.set_spec:
            raise ValueError("--index и --set взаимно исключают — выберите "
                             "одно (для --index значение задаёт --set-text)")
        needle, set_text = "", ""
        if args.set_spec:
            needle, set_text = parse_set_arg(args.set_spec)
        report, patterns = probe_full(args.pid)
        if args.index >= 0:
            report["set_result"] = run_set_index(
                report["controls"], patterns, args.index,
                args.set_text_arg)
        elif args.set_spec:
            if args.dry_write:
                report["dry_write"] = dry_write_result(
                    match_control(report["controls"], needle),
                    needle, set_text)
            else:
                report["set_result"] = run_set(
                    report["controls"], patterns, needle, set_text)
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        print("uia_value_probe: %s" % e, file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
