# -*- coding: utf-8 -*-
"""Прогон YAXUnit из командной строки (IDEAS №22) — без конфигуратора.

Запуск: 1cv8.exe ENTERPRISE /F <иб> /Execute <YAxUnit.epf> /C<конфиг.json>.
Конфиг YAXUnit (JSON) пишется сам: closeAfterTests=true (клиент закрывается сам
после прогонов), reportFormat=jUnit, reportPath. Ожидание завершения процесса с
таймаутом-сторожем; отчёт парсится (jUnit XML) → сводка.

Ловушки (из навыка yaxunit-autotests): клиент может виснуть на «Отчёт об ошибке»
(не компилируемый код падает ДО журнала) — сторож убивает процесс и это
отражается в результате; один прогон на станцию — обвязка не убивает чужие
клиенты, только свой по таймауту.

Формат конфига YAXUnit и пути к epf — на стенде владельца; живой прогон —
skipUnless по наличию epf и базы.
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


class YaxunitError(RuntimeError):
    pass


def find_1cv8() -> str:
    root = Path(r"C:\Program Files\1cv8")
    best = None
    if root.is_dir():
        for d in root.iterdir():
            exe = d / "bin" / "1cv8.exe"
            if exe.is_file():
                key = [int(x) for x in d.name.split(".") if x.isdigit()]
                if best is None or key > best[0]:
                    best = (key, str(exe))
    if not best:
        raise YaxunitError("платформа 1С не найдена под C:\\Program Files\\1cv8")
    return best[1]


def build_config(report_path: str, tests_path: str | None = None) -> dict:
    """Минимальный конфиг YAXUnit: jUnit-отчёт + автозакрытие клиента."""
    cfg = {"closeAfterTests": True, "showReport": False,
           "reportFormat": "jUnit", "reportPath": os.path.abspath(report_path)}
    if tests_path:
        cfg["filter"] = {"testsPath": os.path.abspath(tests_path)}
    return cfg


def parse_junit(path: str) -> dict:
    """jUnit XML → сводка: {tests, failures, errors, time_s, failed: [...]}."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    tests = failures = errors = 0
    time_s = 0.0
    failed = []
    for s in suites:
        tests += int(float(s.get("tests", 0)))
        failures += int(float(s.get("failures", 0)))
        errors += int(float(s.get("errors", 0)))
        time_s += float(s.get("time", 0))
        for case in s.iter("testcase"):
            bad = case.find("failure") is not None or case.find("error") is not None
            if bad:
                failed.append({"name": case.get("name"),
                               "classname": case.get("classname")})
    return {"tests": tests, "failures": failures, "errors": errors,
            "time_s": round(time_s, 1), "failed": failed[:50]}


def run(base: str, epf: str, report_path: str, tests_path: str | None = None,
        timeout_s: int = 3600) -> dict:
    base, epf = os.path.abspath(base), os.path.abspath(epf)
    if not os.path.isfile(os.path.join(base, "1Cv8.1CD")):
        raise YaxunitError("ИБ не найдена: " + base)
    if not os.path.isfile(epf):
        raise YaxunitError("YAxUnit.epf не найден: " + epf)
    report_path = os.path.abspath(report_path)
    cfg = build_config(report_path, tests_path)
    cfg_file = report_path + ".config.json"
    io.open(cfg_file, "w", encoding="utf-8").write(
        json.dumps(cfg, ensure_ascii=False, indent=1))
    if os.path.isfile(report_path):
        os.remove(report_path)  # отчёт прошлого прогона не должен маскировать
    exe = find_1cv8()
    cmd = [exe, "ENTERPRISE", "/DisableStartupDialogs", "/F", base,
           "/Execute", epf, "/C" + cfg_file]
    t0 = time.time()
    killed = False
    try:
        p = subprocess.Popen(cmd)
        try:
            p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            p.kill()
            killed = True
    except OSError as e:
        raise YaxunitError("запуск клиента не удался: %s" % e)
    elapsed = round(time.time() - t0, 1)
    if not os.path.isfile(report_path):
        raise YaxunitError(
            "отчёт не создан за %s с (клиент убит по таймауту: %s); вероятная причина — "
            "висящий диалог/ошибка инициализации (см. навык yaxunit-autotests: "
            "проверяйте модули /CheckModules до прогона)" % (elapsed, killed))
    summary = parse_junit(report_path)
    summary.update({"base": base, "elapsed_s": elapsed, "killed_by_timeout": killed,
                    "report": report_path, "ok": summary["failures"] == 0
                    and summary["errors"] == 0 and not killed})
    return summary


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Прогон YAXUnit (ENTERPRISE /Execute)")
    ap.add_argument("--base", required=True, help="файловая ИБ с тестами")
    ap.add_argument("--epf", required=True, help="путь к YAxUnit.epf")
    ap.add_argument("--report", required=True, help="путь jUnit-отчёта")
    ap.add_argument("--tests-path", help="каталог тестов (filter.testsPath)")
    ap.add_argument("--timeout", type=int, default=3600, help="сторож, с")
    a = ap.parse_args(argv)
    try:
        res = run(a.base, a.epf, a.report, a.tests_path, a.timeout)
    except YaxunitError as e:
        print("yaxunit_run: %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
