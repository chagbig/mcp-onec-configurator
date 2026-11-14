# -*- coding: utf-8 -*-
"""Создание/удаление файловых инфобаз 1С (IDEAS №23) — БЕЗ конфигуратора.

create: пакетная команда 1cv8.exe CREATEINFOBASE File=<путь> (обкатана 08.11,
см. probe/). delete: удаление каталога ИБ — ДВУХФАЗНО (№19): confirm=False —
план (существует ли база, файл 1Cv8.1CD, занятость .cfl-замком), confirm=True —
rmtree каталога. Гварды удаления: база существует, НЕ входит в own_bases
(защита MCP-управляемых стендов), явный confirm.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


class IbError(RuntimeError):
    pass


def find_1cv8() -> str:
    """Свежая платформа под C:\\Program Files\\1cv8\\<ver>\\bin\\1cv8.exe."""
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
        raise IbError("платформа 1С не найдена под C:\\Program Files\\1cv8")
    return best[1]


def _create_raw(path: str) -> None:
    exe = find_1cv8()
    r = subprocess.run([exe, "CREATEINFOBASE", "File=" + path],
                       capture_output=True, timeout=120)
    if r.returncode != 0 or not os.path.isfile(os.path.join(path, "1Cv8.1CD")):
        raise IbError("CREATEINFOBASE rc=%d (%s)" % (r.returncode, path))


def create(path: str) -> dict:
    """CREATEINFOBASE не переваривает ПРОБЕЛЫ в пути (кавычки не спасают —
    изоляция 11.11: ascii+пробел rc=1 при кириллице-без-пробела rc=0), поэтому
    путь с пробелами/кавычками создаётся под временным ASCII-именем в ТОМ ЖЕ
    каталоге и переименовывается (файловая ИБ не хранит каталог у себя)."""
    path = os.path.abspath(path)
    if os.path.isfile(os.path.join(path, "1Cv8.1CD")):
        return {"created": False, "already_exists": True, "path": path}
    safe = (not any(ch in path for ch in ' \t"')) and path.isascii()
    if safe:
        _create_raw(path)
        return {"created": True, "path": path}
    parent = os.path.dirname(path) or "."
    tmp_sib = os.path.join(parent, "ibcreate-%x" % os.urandom(4).__hash__())
    try:
        _create_raw(tmp_sib)
        os.rename(tmp_sib, path)
    finally:
        if os.path.isdir(tmp_sib):
            shutil.rmtree(tmp_sib, ignore_errors=True)
    if not os.path.isfile(os.path.join(path, "1Cv8.1CD")):
        raise IbError("создание через rename не удалось: " + path)
    return {"created": True, "path": path, "via": "temp-rename"}


def plan_delete(path: str, own_bases: list | None = None) -> dict:
    """План удаления (confirm=False): существование, занятость, гварды."""
    path = os.path.abspath(path)
    plan: dict = {"confirm_required": True, "action": "infobase.delete", "path": path}
    ib = os.path.join(path, "1Cv8.1CD")
    plan["is_infobase"] = os.path.isfile(ib)
    if not os.path.isdir(path):
        plan["warning"] = "каталог не существует — удалять нечего"
        plan["next"] = "confirm=true бессмысленно"
        return plan
    if not plan["is_infobase"]:
        plan["warning"] = ("в каталоге нет 1Cv8.1CD — это не инфобаза; удаление "
                           "отклонено (гвард)")
        plan["next"] = "подайте точный каталог ИБ"
        return plan
    if os.path.isfile(ib + ".cfl"):
        plan["warning"] = ("есть 1Cv8.1CD.cfl — база занята конфигуратором/клиентом; "
                           "закройте её")

    def _norm(p):
        try:
            return os.path.normcase(os.path.normpath(p))
        except Exception:
            return (p or "").lower()

    target = _norm(path)
    for ob in (own_bases or []):
        ob_path = str(ob).split("File=", 1)[-1].strip('; "')
        ob_norm = _norm(ob_path)
        if target == ob_norm or target.startswith(ob_norm + os.sep) \
                or ob_norm.startswith(target + os.sep):
            plan["warning"] = "каталог входит в own_bases сервера — УДАЛЕНИЕ ЗАПРЕЩЕНО (гвард)"
            plan["next"] = "own_bases защищены; удаление невозможно"
            return plan
    plan["next"] = "повторите с confirm=true для удаления каталога целиком"
    return plan


def delete(path: str, own_bases: list | None = None) -> dict:
    plan = plan_delete(path, own_bases)
    if "УДАЛЕНИЕ ЗАПРЕЩЕНО" in (plan.get("warning") or "") or not plan.get("is_infobase"):
        raise IbError(plan.get("warning") or "гварды не пройдены")
    try:
        shutil.rmtree(plan["path"])
    except PermissionError as e:
        raise IbError("каталог занят (конфигуратор открыт?): %s" % e)
    return {"deleted": True, "path": plan["path"]}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Создание/удаление файловых ИБ")
    ap.add_argument("--own-bases", help="файл конфига с own_bases (защита)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_c = sub.add_parser("create", help="создать инфобазу")
    p_c.add_argument("path")
    p_d = sub.add_parser("delete", help="удалить инфобазу (двухфазно)")
    p_d.add_argument("path")
    p_d.add_argument("--confirm", action="store_true")
    args = ap.parse_args(argv)

    own = None
    if args.own_bases and os.path.isfile(args.own_bases):
        try:
            own = json.load(io.open(args.own_bases, encoding="utf-8-sig")).get("own_bases")
        except Exception:
            own = None
    try:
        if args.cmd == "create":
            res = create(args.path)
        elif args.confirm:
            res = delete(args.path, own)
        else:
            res = plan_delete(args.path, own)
    except IbError as e:
        print("ib_mgmt: %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
