# -*- coding: utf-8 -*-
"""renovate_ext — MVP-оркестратор идеи #8 «Renovate для расширений 1С» (план
/y/idei/Doc/8-renovate-mcp-plan.md, фаза 3): ОДИН прогон готового конвейера
над парой cf (эталон вендора → доработанная):

  recdiff   → cf_recdiff.py  (быстрый триаж: счётчики записей, кандидаты) — БЕЗ платформы
  diff      → cf_diff.py     (гибридный дифф: переносимое/непереносимое) — ПЛАТФОРМА
  build_ext → build_ext.py   (конвейер 14–15: артефакт расширения .cfe) — ПЛАТФОРМА
  arbiter   → cf_arbiter.py  (выборочная верификация кандидатов /MergeCfg) — ПЛАТФОРМА

Это CLI-инструмент (как cf_arbiter), НЕ MCP-инструмент: один вызов = один
прогон, планировщик снаружи (cron/задача планировщика). Оркестратор НЕ
дублирует логику инструментов — вызывает их subprocess'ом и проверяет
ПОСТУСЛОВИЯ каждой стадии; семантика attempts/last_error идеи #431: никаких
молчаливых пропусков — skipped ОБЯЗАТЕЛЬНО с причиной (reason в detail).

Статусы шагов: ok | skipped (с причиной) | failed. Fail-fast: failed шаг
останавливает пайплайн, но отчёт (work/report.json + work/report.md) пишется
всегда. Арбитр rc=1 (differs) — НЕ фейл пайплайна, а вердикт «спорные требуют
человека». rc CLI: 0 — обязательные шаги ok, 1 — есть failed, 2 — ошибка
аргументов.

Короткое замыкание: если cf идентичны (recdiff rc=0 и нулевые счётчики),
остальные шаги помечаются skipped с причиной «cf идентичны» — часы слотов и
выгрузок не тратятся впустую.

Инъекция шагов (тесты, образец — init_base/merge_cfg/dump_cfg в
test_cf_arbiter.py): run_step={"recdiff": {...}, ...} — dict шаг → fake-результат
(реальный subprocess не запускается, платформа не нужна; значения могут быть
callable без аргументов, возвращающими такой dict). Контракт fake:

  recdiff:   {"rc": 0|2, "counts": {"new","deleted","changed","recreated"},
              "candidates": [...], "records1": int, "records2": int}
  diff:      {"rc": 0, "counts": {"changed_files","new_files","deleted_files",
              "new_objs"}, "transferable": int, "non_transferable": int,
              "transfer": [...], "manual": [...]}
  build_ext: {"rc": 0, "cfe": "<путь к СУЩЕСТВУЮЩЕМУ файлу-артефакту>",
              "vendor_ok": True}
  arbiter:   {"rc": 0|1|2, "identical": int, "differs": int, "missing": int}

Постусловия проверяются и на fake: rc=0 build_ext без существующего cfe-файла
→ failed (не молчаливый пропуск).

Cleanup (keep=False): тяжёлые промежуточные каталоги (cfdiff — слоты и полные
XML-выгрузки, build_ext, arbiter) удаляются; report.json/md, recdiff.json,
diff.json и cfe-артефакт (копия в корне work) остаются. После failed шага
cleanup НЕ выполняется — улики для диагностики.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

TOOLS = Path(__file__).resolve().parent

STEPS = ("recdiff", "diff", "build_ext", "arbiter")

# таймауты subprocess per-step, с (ночные прогоны на 2 ГБ паре — часы)
STEP_TIMEOUTS = {"recdiff": 3600, "diff": 14400, "build_ext": 28800, "arbiter": 14400}

# cf_recdiff сигнализирует «есть различия» кодом 2 (0 = идентичны) — оба успешные
RECDIFF_ACCEPT_RC = (0, 2)


class RenovateError(ValueError):
    pass


# --------------------------------------------------------------------------
# 0. Платформа
# --------------------------------------------------------------------------

def _find_1cv8() -> str | None:
    """Новейший 1cv8.exe под C:\\Program Files\\1cv8\\<версия>\\bin — образ
    yaxunit_run.find_1cv8 / enterprise_smoke.find_1cv8c. None = не найдена."""
    root = Path(r"C:\Program Files\1cv8")
    best = None
    if root.is_dir():
        for d in root.iterdir():
            exe = d / "bin" / "1cv8.exe"
            if exe.is_file():
                key = [int(x) for x in d.name.split(".") if x.isdigit()]
                if best is None or key > best[0]:
                    best = (key, str(exe))
    return best[1] if best else None


# --------------------------------------------------------------------------
# 1. Вспомогательные
# --------------------------------------------------------------------------

def _run(cmd: list, timeout: int, work: Path, tag: str) -> tuple:
    """subprocess с capture; stdout/stderr целиком — в work/<tag>.{out,err}.log
    (улики для диагностики), возвращает (rc, stdout, stderr)."""
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (work / (tag + ".out.log")).write_text(proc.stdout or "", encoding="utf-8")
    (work / (tag + ".err.log")).write_text(proc.stderr or "", encoding="utf-8")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _failed(detail: str, **extra) -> dict:
    payload = {"status": "failed", "detail": detail, "last_error": detail}
    payload.update(extra)
    return payload


def _skipped(detail: str, **extra) -> dict:
    payload = {"status": "skipped", "detail": detail, "last_error": None}
    payload.update(extra)
    return payload


def _not_run(step: str, reason: str) -> dict:
    """Шаг не выполнялся (fail-fast / короткое замыкание) — attempts=0,
    причина обязательна (никаких молчаливых пропусков, #431)."""
    return {"step": step, "status": "skipped", "elapsed_s": 0.0,
            "detail": "не выполнялся: %s" % reason, "attempts": 0, "last_error": None}


def _fmt4(c: dict) -> str:
    return "new=%d deleted=%d changed=%d recreated=%d" % (
        c.get("new") or 0, c.get("deleted") or 0,
        c.get("changed") or 0, c.get("recreated") or 0)


def _rc(payload: dict) -> int:
    return int(payload.get("rc") or 0)


# --------------------------------------------------------------------------
# 2. Шаги конвейера (fake=None → реальный subprocess; fake — инъекция тестов)
# --------------------------------------------------------------------------

def _step_recdiff(cf1: Path, cf2: Path, work: Path, fake) -> dict:
    """cf_recdiff.py --cf1 --cf2 --json work/recdiff.json. Принимаются rc 0
    (идентичны) и 2 (есть различия — штатный сигнал триажа)."""
    if fake is not None:
        payload = dict(fake)
    else:
        report = work / "recdiff.json"
        cmd = [sys.executable, str(TOOLS / "cf_recdiff.py"), "--cf1", str(cf1),
               "--cf2", str(cf2), "--json", str(report)]
        try:
            rc, out, err = _run(cmd, STEP_TIMEOUTS["recdiff"], work, "recdiff")
        except subprocess.TimeoutExpired:
            return _failed("recdiff: таймаут %d с — процесс снят" % STEP_TIMEOUTS["recdiff"], rc=-1)
        except OSError as e:
            return _failed("recdiff: запуск не удался: %s" % e, rc=-1)
        payload = {"rc": rc, "stdout_tail": out[-400:], "stderr_tail": err[-400:]}
        if rc not in RECDIFF_ACCEPT_RC:
            return _failed("cf_recdiff: rc=%d (ожидается 0=идентичны или 2=есть различия; "
                           "см. work/recdiff.*.log)" % rc,
                           stdout_tail=out[-400:], stderr_tail=err[-400:])
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return _failed("recdiff.json не читается: %s" % e, rc=rc)
        payload["counts"] = {k: len(data.get(k) or [])
                             for k in ("new", "deleted", "changed", "recreated")}
        payload["records1"] = data.get("records1")
        payload["records2"] = data.get("records2")
        payload["candidates"] = [e.get("name") for e in (data.get("named") or [])
                                 if e.get("name")][:20]
    counts = {"new": 0, "deleted": 0, "changed": 0, "recreated": 0}
    counts.update(payload.get("counts") or {})
    payload["counts"] = counts
    rc = _rc(payload)
    payload["identical"] = (rc == 0 and not any(counts.values()))
    if rc not in RECDIFF_ACCEPT_RC:
        payload["status"] = "failed"
        payload["last_error"] = payload.get("error") or ("rc=%d (ожидается 0/2)" % rc)
        payload.setdefault("detail", "cf_recdiff failed: %s" % payload["last_error"])
        return payload
    payload["status"] = "ok"
    payload.setdefault("detail", "записей: %s vs %s; %s"
                       % (payload.get("records1") or "?", payload.get("records2") or "?",
                          _fmt4(counts)))
    return payload


def _step_diff(cf1: Path, cf2: Path, work: Path, fake) -> dict:
    """cf_diff.py cf1 cf2 --work work/cfdiff --report work/diff.json — гибридный
    дифф (слоты и выгрузки кэшируются по sha1 cf). cf_diff сейчас игнорирует
    --report и пишет diff.json в свой work-каталог — отчёт дублируется в корень
    work (чтобы пережил cleanup cfdiff при keep=False)."""
    if fake is not None:
        payload = dict(fake)
    else:
        cfdiff = work / "cfdiff"
        cmd = [sys.executable, str(TOOLS / "cf_diff.py"), str(cf1), str(cf2),
               "--work", str(cfdiff), "--report", str(work / "diff.json")]
        try:
            rc, out, err = _run(cmd, STEP_TIMEOUTS["diff"], work, "diff")
        except subprocess.TimeoutExpired:
            return _failed("diff: таймаут %d с — процесс снят" % STEP_TIMEOUTS["diff"], rc=-1)
        except OSError as e:
            return _failed("diff: запуск не удался: %s" % e, rc=-1)
        payload = {"rc": rc, "stdout_tail": out[-400:], "stderr_tail": err[-400:]}
        if rc != 0:
            return _failed("cf_diff: rc=%d (см. work/diff.*.log)" % rc,
                           stdout_tail=out[-400:], stderr_tail=err[-400:])
        report = work / "diff.json"
        if not report.is_file():
            report = cfdiff / "diff.json"
        if not report.is_file():
            return _failed("diff.json не найден ни в work, ни в work/cfdiff", rc=rc)
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return _failed("diff.json не читается: %s" % e, rc=rc)
        fd = data.get("full_diff") or {}
        v = data.get("verdicts") or {}
        payload["counts"] = {
            "changed_files": len(fd.get("changed") or []),
            "new_files": len(fd.get("only2") or []),
            "deleted_files": len(fd.get("only1") or []),
            "new_objs": len(fd.get("new_objs") or []),
        }
        payload["transferable"] = len(v.get("transfer") or [])
        payload["non_transferable"] = len(v.get("manual") or [])
        payload["transfer"] = (v.get("transfer") or [])[:20]
        payload["manual"] = (v.get("manual") or [])[:20]
        payload["mode"] = data.get("mode")
        if report != work / "diff.json":
            shutil.copy2(report, work / "diff.json")
    counts = {"changed_files": 0, "new_files": 0, "deleted_files": 0, "new_objs": 0}
    counts.update(payload.get("counts") or {})
    payload["counts"] = counts
    if _rc(payload) != 0:
        payload["status"] = "failed"
        payload["last_error"] = payload.get("error") or ("rc=%d (ожидается 0)" % payload.get("rc"))
        payload.setdefault("detail", "cf_diff failed: %s" % payload["last_error"])
        return payload
    payload["status"] = "ok"
    payload.setdefault("detail",
                       "файлов: changed=%(changed_files)d new=%(new_files)d "
                       "deleted=%(deleted_files)d (новых объектов: %(new_objs)d); "
                       "переносимое=%(t)d непереносимое=%(n)d"
                       % dict(counts, t=payload.get("transferable") or 0,
                              n=payload.get("non_transferable") or 0))
    return payload


def _step_build_ext(cf1: Path, cf2: Path, work: Path, name: str, prefix: str,
                    platform, fake) -> dict:
    """build_ext.py cf1 cf2 --name --prefix --work work/build_ext. ПЛАТФОРМА:
    без 1cv8.exe шаг skipped (не failed). Постусловие: cfe-артефакт существует
    и копируется в корень work (переживает cleanup build_ext при keep=False)."""
    if fake is None and not platform:
        return _skipped("платформа 1С не найдена (C:\\Program Files\\1cv8\\<версия>"
                        "\\bin\\1cv8.exe) — сборка расширения (DESIGNER) невозможна")
    bwork = work / "build_ext"
    if fake is not None:
        payload = dict(fake)
    else:
        cmd = [sys.executable, str(TOOLS / "build_ext.py"), str(cf1), str(cf2),
               "--name", name, "--prefix", prefix, "--work", str(bwork)]
        try:
            rc, out, err = _run(cmd, STEP_TIMEOUTS["build_ext"], work, "build_ext")
        except subprocess.TimeoutExpired:
            return _failed("build_ext: таймаут %d с — процесс снят" % STEP_TIMEOUTS["build_ext"], rc=-1)
        except OSError as e:
            return _failed("build_ext: запуск не удался: %s" % e, rc=-1)
        payload = {"rc": rc, "stdout_tail": out[-400:], "stderr_tail": err[-400:]}
        if rc != 0:
            return _failed("build_ext: rc=%d (см. work/build_ext.*.log и "
                           "build_ext/report.json)" % rc,
                           stdout_tail=out[-400:], stderr_tail=err[-400:])
        rep_path = bwork / "report.json"
        if rep_path.is_file():
            try:
                brep = json.loads(rep_path.read_text(encoding="utf-8"))
                payload["vendor_ok"] = brep.get("vendor_ok")
                fill = brep.get("fill") or {}
                payload["inserted"] = fill.get("inserted")
                payload["mapping"] = len(fill.get("mapping") or [])
                payload["build_report"] = str(rep_path)
                if brep.get("cfe"):
                    payload.setdefault("cfe", brep["cfe"])
            except (OSError, ValueError):
                pass  # отчёт вторичен — постусловие артефакта проверяется ниже
    if _rc(payload) != 0:
        payload["status"] = "failed"
        payload["last_error"] = payload.get("error") or ("rc=%d (ожидается 0)" % payload.get("rc"))
        payload.setdefault("detail", "build_ext failed: %s" % payload["last_error"])
        return payload
    cfe_src = Path(payload["cfe"]) if payload.get("cfe") else bwork / (name + ".cfe")
    if not cfe_src.is_file():
        payload["status"] = "failed"
        payload["last_error"] = "постусловие: cfe-артефакт не построен: %s" % cfe_src
        payload["detail"] = payload["last_error"]
        return payload
    artifact = work / (name + ".cfe")
    if cfe_src.resolve() != artifact.resolve():
        shutil.copy2(cfe_src, artifact)
    payload["artifact"] = str(artifact)
    payload["status"] = "ok"
    payload.setdefault("detail", "cfe: %s (vendor_ok=%s, вставок=%s)"
                       % (artifact, payload.get("vendor_ok"), payload.get("inserted")))
    return payload


def _step_arbiter(cf1: Path, cf2: Path, work: Path, max_arbiter: int, platform,
                  rec_payload, fake) -> dict:
    """cf_arbiter.py --cf1 --cf2 --recdiff work/recdiff.json --max-n --work
    work/arbiter — опциональный шаг по кандидатам recdiff. rc 0 — все
    identical; rc 1 — differs/missing: НЕ фейл, а «спорные требуют человека»;
    rc 2 или нет платформы — skipped с причиной."""
    counts = (rec_payload or {}).get("counts") or {}
    candidates = sum(int(counts.get(k) or 0) for k in ("new", "deleted", "changed"))
    if candidates <= 0:
        return _skipped("нет кандидатов из recdiff — арбитраж не нужен")
    if fake is not None:
        payload = dict(fake)
        rc = _rc(payload)
        if rc not in (0, 1, 2):
            payload["status"] = "failed"
            payload["last_error"] = payload.get("error") or ("rc=%d (ожидается 0/1/2)" % rc)
            payload.setdefault("detail", "cf_arbiter failed: %s" % payload["last_error"])
            return payload
        if rc == 2:
            payload["status"] = "skipped"
            payload.setdefault("detail", "cf_arbiter отказ (rc=2): %s"
                               % (payload.get("error") or "см. шаг detail"))
            return payload
    else:
        if not platform:
            return _skipped("нет платформы — арбитраж (/MergeCfg на временной базе) невозможен")
        recdot = work / "recdiff.json"
        if not recdot.is_file():
            return _skipped("work/recdiff.json не найден (recdiff был инъекцией?) — "
                            "кандидаты не передать")
        cmd = [sys.executable, str(TOOLS / "cf_arbiter.py"), "--cf1", str(cf1),
               "--cf2", str(cf2), "--recdiff", str(recdot),
               "--max-n", str(int(max_arbiter)), "--work", str(work / "arbiter")]
        try:
            rc, out, err = _run(cmd, STEP_TIMEOUTS["arbiter"], work, "arbiter")
        except subprocess.TimeoutExpired:
            return _failed("arbiter: таймаут %d с — процесс снят" % STEP_TIMEOUTS["arbiter"], rc=-1)
        except OSError as e:
            return _failed("arbiter: запуск не удался: %s" % e, rc=-1)
        payload = {"rc": rc, "stdout_tail": out[-400:], "stderr_tail": err[-400:]}
        for marker, key in (("ИДЕНТИЧНО", "identical"), ("РАЗЛИЧИЕ", "differs"),
                            ("ОТСУТСТВУЕТ", "missing")):
            payload[key] = sum(1 for ln in out.splitlines()
                               if ln.strip().startswith(marker))
        if rc not in (0, 1, 2):
            return _failed("cf_arbiter: rc=%d (см. work/arbiter.*.log)" % rc,
                           stdout_tail=out[-400:], stderr_tail=err[-400:])
        if rc == 2:
            return _skipped("cf_arbiter отказ (rc=2): %s"
                            % ((err or out).strip()[-200:] or "см. work/arbiter.*.log"),
                            stdout_tail=out[-400:], stderr_tail=err[-400:])
    payload.setdefault("identical", 0)
    payload.setdefault("differs", 0)
    payload.setdefault("missing", 0)
    payload["disputed"] = rc == 1 or bool(payload["differs"]) or bool(payload["missing"])
    payload["status"] = "ok"
    payload.setdefault("detail", "identical=%d differs=%d missing=%d%s"
                       % (payload["identical"], payload["differs"], payload["missing"],
                          "; спорные требуют человека" if payload["disputed"] else ""))
    return payload


# --------------------------------------------------------------------------
# 2д. git_publish (итерация 2a, решения владельца 14.11.2026: фаза 4.1 вариант б)
# --------------------------------------------------------------------------

def _git_cmd(cwd: Path | None):
    """git subprocess с фиксированной идентичностью (коммиты бота) и utf-8."""
    def g(*args: str) -> tuple[int, str]:
        p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    return g


def _push_url_with_token(git_url: str) -> str:
    token = os.environ.get("GITLAB_TOKEN")
    if token and git_url.startswith("https://") and "@" not in git_url:
        return git_url.replace("https://", "https://oauth2:%s@" % token, 1)
    return git_url


def _step_git_publish(result: dict, work_dir: Path, git_url: str, git_branch: str,
                      mr_target: str, mr_fn, fake) -> dict:
    """Ветка renovate/<name> в git-репо: cfe + report.json/md коммитом, push,
    MR через GitLab API (mr_fn — инъекция для тестов; в реале — GITLAB_TOKEN).
    Решение владельца 14.11.2026 (фаза 4.1б): результат прогона — в git + MR."""
    if fake is not None:
        payload = dict(fake) if not callable(fake) else fake()
        payload.setdefault("status", "ok")
        return payload
    name = result["name"]
    branch = git_branch or ("renovate/" + name)
    repo_dir = work_dir / "gitrepo"
    # 1. клон (или переиспользование клона в том же work)
    if (repo_dir / ".git").is_dir():
        g = _git_cmd(repo_dir)
        rc, out = g("fetch", "--all", "--prune")
        if rc != 0:
            return _failed("git fetch: %s" % out[-300:])
    else:
        g = _git_cmd(work_dir)
        rc, out = g("clone", _push_url_with_token(git_url), repo_dir.name)
        if rc != 0:
            return _failed("git clone: %s" % out[-300:])
        g = _git_cmd(repo_dir)
    # 2. ветка: продолжаем remote-ветку, если она есть (иначе повторный прогон
    # создаст несвязанную историю и push будет отброшен non-fast-forward)
    rc, _ = g("rev-parse", "--verify", "origin/" + branch)
    rc, out = (g("checkout", "-B", branch, "origin/" + branch) if rc == 0
               else g("checkout", "-B", branch))
    if rc != 0:
        return _failed("git checkout -B %s: %s" % (branch, out[-300:]))
    # 3. файлы прогона
    sub = repo_dir / name
    sub.mkdir(parents=True, exist_ok=True)
    artifact = result.get("artifact")
    copied = []
    if artifact and Path(artifact).is_file():
        shutil.copy2(artifact, sub / Path(artifact).name)
        copied.append(Path(artifact).name)
    for key in ("report_json", "report_md"):
        f = result.get(key)
        if f and Path(f).is_file():
            shutil.copy2(f, sub / Path(f).name)
            copied.append(Path(f).name)
    # 4. коммит (идемпотентно: нечего коммитить — не ошибка)
    g("-c", "user.email=renovate@local", "-c", "user.name=renovate-bot",
      "add", name)
    rc, out = g("-c", "user.email=renovate@local", "-c", "user.name=renovate-bot",
                "commit", "-m", "renovate: %s — %s" % (
                    name, (result.get("verdict") or {}).get("code", "")))
    committed = rc == 0
    if rc != 0 and "nothing to commit" not in out:
        return _failed("git commit: %s" % out[-300:])
    # 5. push (токен из env GITLAB_TOKEN; для локальных путей — как есть)
    rc, out = g("push", "-u", "origin", branch)
    if rc != 0:
        return _failed("git push: %s" % out[-300:],
                       hint="для https-remote задайте GITLAB_TOKEN (oauth2)")
    # 6. MR
    mr = None
    if mr_target:
        token = os.environ.get("GITLAB_TOKEN")
        if not token and mr_fn is None:
            return _failed("MR: нет GITLAB_TOKEN в окружении (ветка %s запушена; "
                           "MR требует токен GitLab)" % branch, branch=branch)
        verdict_code = (result.get("verdict") or {}).get("code", "")
        if mr_fn is not None:
            mr = mr_fn(branch, mr_target, result)
        else:
            m = re.match(r"https://([^/]+)/(.+?)(?:\.git)/?$", git_url)
            if not m:
                return _failed("MR: не разобрать git URL: %s" % git_url, branch=branch)
            api = "https://%s/api/v4/projects/%s/merge_requests" % (
                m.group(1), urllib.parse.quote(m.group(2), safe=""))
            body = json.dumps({"source_branch": branch, "target_branch": mr_target,
                               "title": "renovate: %s (%s)" % (name, verdict_code),
                               "remove_source_branch": False}).encode("utf-8")
            req = urllib.request.Request(api, data=body, method="POST", headers={
                "PRIVATE-TOKEN": token, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                mr = {"url": data.get("web_url"), "iid": data.get("iid")}
            except Exception as e:
                return _failed("MR: %s: %s" % (type(e).__name__, e), branch=branch)
        if isinstance(mr, dict) and mr.get("error"):
            return _failed("MR: %s" % mr["error"], branch=branch)
        if mr is None:
            return _failed("MR: инъекция вернула None", branch=branch)
    detail = "ветка %s запушена%s%s" % (
        branch, "; коммит: %s" % ("есть" if committed else "не потребовался"),
        (", MR: %s" % mr["url"]) if isinstance(mr, dict) and mr.get("url") else "")
    return {"status": "ok", "branch": branch, "committed": committed,
            "mr": mr, "copied": copied, "detail": detail}


# --------------------------------------------------------------------------
# 3. Пайплайн
# --------------------------------------------------------------------------

def _compute_verdict(result: dict) -> None:
    failed = [e["step"] for e in result["steps"] if e["status"] == "failed"]
    if failed:
        result["verdict"] = {"code": "failed",
                             "text": "пайплайн упал на шагах: %s" % ", ".join(failed)}
        return
    if (result["recdiff"] or {}).get("identical"):
        result["verdict"] = {"code": "no_changes",
                             "text": "cf идентичны (recdiff) — перенос не требуется"}
        return
    arb = result["arbiter"] or {}
    if arb.get("disputed"):
        result["verdict"] = {"code": "disputed",
                             "text": "спорные требуют человека (арбитр: differs=%s, "
                                     "missing=%s)" % (arb.get("differs"), arb.get("missing"))}
        return
    be = result["build_ext"] or {}
    if be.get("status") == "skipped":
        result["verdict"] = {"code": "partial",
                             "text": "анализ завершён; сборка расширения пропущена: %s"
                                     % be.get("detail", "")}
        return
    result["verdict"] = {"code": "ok",
                         "text": "расширение собрано: %s"
                                 % (result["artifact"] or "артефакт не обнаружен")}


def _write_report(result: dict, work: Path) -> None:
    report_json = work / "report.json"
    report_json.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    result["report_json"] = str(report_json)
    report_md = work / "report.md"
    report_md.write_text(_render_md(result), encoding="utf-8")
    result["report_md"] = str(report_md)


def _cleanup(work: Path, result: dict) -> None:
    """Тяжёлые промежуточные каталоги (слоты, выгрузки, временные базы) — долой;
    отчёты, recdiff.json/diff.json и cfe-артефакт остаются в корне work.
    gitrepo НЕ удаляется: клон переиспользуется между прогонами (fetch обновляет;
    rmtree на Windows падал молча на залоченном .git — частичное удаление
    ломало следующий fetch, v1.11)."""
    for sub in ("cfdiff", "build_ext", "arbiter"):
        d = work / sub
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            result["cleaned"].append(str(d))


def pipeline(cf1, cf2, work="", name="МсрДиф", prefix="Мср_", max_arbiter=10,
             keep=False, *, run_step=None, git_url="", git_branch="",
             mr_target="", mr_fn=None) -> dict:
    """Один прогон конвейера renovate над парой cf. Шаги последовательно
    (STEPS), fail-fast: failed шаг останавливает пайплайн, отчёт всё равно
    пишется (finally). Возвращает итоговый dict — он же уходит в report.json.

    run_step — инъекция шагов для тестов (см. докстринг модуля); None →
    реальные subprocess-шаги.

    git_url — задан → после основных шагов выполняется git_publish (итерация
    2a, решение владельца фаза 4.1б): ветка git_branch (умолчание
    renovate/<name>), коммит cfe + отчётов, push (токен GITLAB_TOKEN для
    https), MR в mr_target (mr_fn — инъекция; без неё нужен GITLAB_TOKEN).
    """
    t0 = time.time()
    run_step = dict(run_step or {})
    cf1p, cf2p = Path(cf1), Path(cf2)
    for p in (cf1p, cf2p):
        if not p.is_file():
            raise RenovateError("cf не найден: %s" % p)
    if work:
        work_dir = Path(work)
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="renovate-ext-"))
    result = {
        "cf1": str(cf1p), "cf2": str(cf2p), "work": str(work_dir),
        "name": name, "prefix": prefix, "max_arbiter": int(max_arbiter),
        "keep": bool(keep), "platform": _find_1cv8(),
        "steps": [], "recdiff": None, "diff": None, "build_ext": None,
        "arbiter": None, "artifact": None, "verdict": None,
        "git_publish": None, "git_url": git_url or None,
        "elapsed_s": 0.0, "report_json": None, "report_md": None, "cleaned": [],
    }
    runners = {
        "recdiff": lambda fake: _step_recdiff(cf1p, cf2p, work_dir, fake),
        "diff": lambda fake: _step_diff(cf1p, cf2p, work_dir, fake),
        "build_ext": lambda fake: _step_build_ext(cf1p, cf2p, work_dir, name, prefix,
                                                  result["platform"], fake),
        "arbiter": lambda fake: _step_arbiter(cf1p, cf2p, work_dir, max_arbiter,
                                              result["platform"], result["recdiff"], fake),
    }
    stopped = None
    try:
        for step_name in STEPS:
            if stopped is not None:
                result["steps"].append(_not_run(step_name,
                                                "пайплайн остановлен failed-шагом '%s'" % stopped))
                continue
            t = time.time()
            try:
                fake = run_step.get(step_name)
                payload = runners[step_name](fake() if callable(fake) else fake)
            except Exception as e:  # ошибка хендлера — failed шаг, не молчаливый пропуск
                payload = _failed("%s: %s: %s" % (step_name, type(e).__name__, e))
            entry = {
                "step": step_name, "status": payload["status"],
                "elapsed_s": round(time.time() - t, 2),
                "detail": str(payload.get("detail", "")),
                "attempts": 1, "last_error": payload.get("last_error"),
            }
            result["steps"].append(entry)
            result[step_name] = payload
            if payload["status"] == "failed":
                stopped = step_name
            elif step_name == "build_ext" and payload.get("artifact"):
                result["artifact"] = payload["artifact"]
            elif step_name == "recdiff" and payload.get("identical"):
                for rest in STEPS[1:]:
                    result["steps"].append(_not_run(
                        rest, "cf идентичны (recdiff): нет кандидатов — "
                              "анализ и перенос не требуются"))
                break
        _compute_verdict(result)
    finally:
        try:
            _write_report(result, work_dir)
            # git_publish (итерация 2a): после отчётов — коммитит их же
            if git_url:
                t = time.time()
                if stopped is not None:
                    gp = _skipped("пайплайн остановлен failed-шагом '%s'" % stopped)
                elif not result.get("artifact"):
                    gp = _skipped("нет cfe-артефакта (build_ext не собирал "
                                  "расширение) — публиковать нечего")
                else:
                    try:
                        fake = run_step.get("git_publish")
                        gp = _step_git_publish(result, work_dir, git_url,
                                               git_branch, mr_target, mr_fn,
                                               fake() if callable(fake) else fake)
                    except Exception as e:
                        gp = _failed("git_publish: %s: %s" % (type(e).__name__, e))
                gp["step"] = "git_publish"
                gp.setdefault("attempts", 1)
                gp.setdefault("elapsed_s", round(time.time() - t, 2))
                result["git_publish"] = gp
                result["steps"].append({
                    "step": "git_publish", "status": gp["status"],
                    "elapsed_s": gp["elapsed_s"], "detail": str(gp.get("detail", "")),
                    "attempts": gp["attempts"], "last_error": gp.get("last_error")})
                _write_report(result, work_dir)  # отчёт теперь с git_publish
        finally:
            if not keep and stopped is None:
                _cleanup(work_dir, result)
            result["elapsed_s"] = round(time.time() - t0, 2)
    return result


# --------------------------------------------------------------------------
# 4. Человекочитаемый отчёт
# --------------------------------------------------------------------------

def _render_md(r: dict) -> str:
    v = r.get("verdict") or {}
    lines = [
        "# Renovate расширений — отчёт",
        "",
        "- Вход: cf1=`%s`, cf2=`%s`" % (r["cf1"], r["cf2"]),
        "- Расширение: name=%s, prefix=%s; кандидатов арбитру: %d"
        % (r["name"], r["prefix"], r["max_arbiter"]),
        "- Платформа: %s" % (r.get("platform") or "не найдена"),
        "- Вердикт: **%s** — %s" % (v.get("code"), v.get("text")),
        "- Артефакт: %s" % (r.get("artifact") or "отсутствует"),
        "- Work: %s" % r["work"],
        "",
        "## Шаги",
        "",
        "| Шаг | Статус | сек | Детали |",
        "|---|---|---|---|",
    ]
    for e in r["steps"]:
        d = e.get("detail") or ""
        if e.get("last_error"):
            d += " **ошибка:** %s" % e["last_error"]
        lines.append("| %s | %s | %s | %s |"
                     % (e["step"], e["status"], e["elapsed_s"],
                        str(d).replace("|", "\\|")))
    rec = r.get("recdiff")
    lines += ["", "## Различия (cf_recdiff)", ""]
    if rec:
        lines.append("записей: %s vs %s; %s"
                     % (rec.get("records1") or "?", rec.get("records2") or "?",
                        _fmt4(rec.get("counts") or {})))
        if rec.get("identical"):
            lines.append("cf идентичны — различий нет.")
        cands = rec.get("candidates") or []
        if cands:
            lines.append("")
            lines.append("Кандидаты (имена из сериализаций): " + ", ".join(str(c) for c in cands))
    else:
        lines.append("нет данных (шаг не выполнялся).")
    diff = r.get("diff")
    lines += ["", "## Diff (cf_diff)", ""]
    if diff:
        c = diff.get("counts") or {}
        lines.append("файлов: changed=%s new=%s deleted=%s (новых объектов: %s); "
                     "переносимое=%s, непереносимое=%s"
                     % (c.get("changed_files"), c.get("new_files"), c.get("deleted_files"),
                        c.get("new_objs"), diff.get("transferable"),
                        diff.get("non_transferable")))
        for t in (diff.get("transfer") or []):
            lines.append("- TRANSFER: %s → %s" % (t.get("path"), t.get("action")))
        for m in (diff.get("manual") or []):
            lines.append("- MANUAL: %s (%s)" % (m.get("path"), m.get("class")))
    else:
        lines.append("нет данных (шаг не выполнялся).")
    be = r.get("build_ext")
    lines += ["", "## Расширение (build_ext)", ""]
    if be:
        lines.append("статус: %s; артефакт: %s; vendor_ok: %s"
                     % (be.get("status"), be.get("artifact") or be.get("cfe") or "-",
                        be.get("vendor_ok")))
    else:
        lines.append("нет данных (шаг пропущен или не выполнялся).")
    arb = r.get("arbiter")
    lines += ["", "## Арбитр (cf_arbiter)", ""]
    if arb:
        lines.append("identical=%s differs=%s missing=%s; спорные: %s"
                     % (arb.get("identical"), arb.get("differs"), arb.get("missing"),
                        "ДА — требуется человек" if arb.get("disputed") else "нет"))
    else:
        lines.append("нет данных (шаг пропущен или не выполнялся).")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 5. CLI
# --------------------------------------------------------------------------

def main(argv=None, run_step=None) -> int:
    for stream in (sys.stdout, sys.stderr):   # образец blog_publish.py
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description="renovate_ext: recdiff → diff → build_ext → arbiter — один прогон")
    ap.add_argument("cf1", help="вендорский эталон (cf)")
    ap.add_argument("cf2", help="доработанная конфигурация (cf)")
    ap.add_argument("--name", default="МсрДиф", help="имя расширения")
    ap.add_argument("--prefix", default="Мср_", help="префикс собственных объектов")
    ap.add_argument("--max-arbiter", type=int, default=10,
                    help="лимит кандидатов арбитру из recdiff")
    ap.add_argument("--work", default="", help="рабочий каталог (по умолчанию — временный)")
    ap.add_argument("--keep", action="store_true",
                    help="не удалять тяжёлые промежуточные каталоги (cfdiff/build_ext/arbiter/gitrepo)")
    ap.add_argument("--git-url", default="",
                    help="git-репо результата (итерация 2a): ветка renovate/<name>, "
                         "коммит cfe+отчётов, push; пусто = без git-шага")
    ap.add_argument("--git-branch", default="",
                    help="ветка публикации (умолчание renovate/<name>)")
    ap.add_argument("--mr-target", default="",
                    help="целевая ветка MR (GitLab API, нужен GITLAB_TOKEN); "
                         "пусто = без MR")
    a = ap.parse_args(argv)
    for p in (a.cf1, a.cf2):
        if not Path(p).is_file():
            print("ОТКАЗ: cf не найден: %s" % p, file=sys.stderr)
            return 2
    try:
        rep = pipeline(a.cf1, a.cf2, work=a.work, name=a.name, prefix=a.prefix,
                       max_arbiter=a.max_arbiter, keep=a.keep, run_step=run_step,
                       git_url=a.git_url, git_branch=a.git_branch,
                       mr_target=a.mr_target)
    except (RenovateError, OSError) as e:
        print("ОТКАЗ: %s" % e, file=sys.stderr)
        return 2
    v = rep["verdict"] or {}
    print("вердикт: %s — %s" % (v.get("code"), v.get("text")))
    for e in rep["steps"]:
        line = "  %-9s %-9s (%s с)" % (e["status"], e["step"], e["elapsed_s"])
        if e["detail"]:
            line += ": " + e["detail"][:120]
        if e["last_error"]:
            line += " [ошибка: %s]" % str(e["last_error"])[:120]
        print(line)
    print("артефакт:", rep["artifact"] or "отсутствует")
    print("отчёты:", rep["report_json"], "|", rep["report_md"])
    return 1 if any(e["status"] == "failed" for e in rep["steps"]) else 0


if __name__ == "__main__":
    sys.exit(main())
