# -*- coding: utf-8 -*-
"""Итерация 2a (решение владельца фаза 4.1б, 14.11.2026): git_publish-шаг
renovate_ext — ветка renovate/<name> в git-репо, коммит cfe+отчётов, push,
MR через инъекцию. Реальный git — только с локальным bare-remote (CI/локально
безопасно; MR по сети не тестируется)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import renovate_ext  # noqa: E402
from test_renovate_ext import _ok_fakes, _mkcf, _default_objects  # noqa: E402


def _git(cwd: Path, *args: str) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return p.returncode, (p.stdout or "") + (p.stderr or "")


class GitFixture(unittest.TestCase):
    """bare-remote + пара cf + ok-фейки без recdiff (реальный recdiff-шаг
    не нужен — git_publish инъекцией или реальным git)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="renovate-git-test-")
        self.d = Path(self._tmp.name)
        self.bare = self.d / "remote.git"
        _git(self.d, "init", "--bare", "--initial-branch=main", self.bare.name)
        self.cf1 = _mkcf(self.d, "cf1.cf", _default_objects())
        self.cf2 = _mkcf(self.d, "cf2.cf",
                         _default_objects() + [("uuid-new", "НовыйОбъект", {})])
        self.work = self.d / "run"
        self.work.mkdir()
        # no-platform гейт (как в test_renovate_ext._TmpDirTestCase)
        patcher = mock.patch.object(renovate_ext, "_find_1cv8", return_value="fake")
        patcher.start()
        self.addCleanup(patcher.stop)
        # токена нет ни в одном тесте, если он не задан явно
        self._token_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._token_patcher.start()
        os.environ.pop("GITLAB_TOKEN", None)
        self.addCleanup(self._token_patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fakes_no_recdiff(self, work: Path) -> dict:
        fakes = _ok_fakes(work)
        fakes.pop("recdiff", None)
        return fakes

    def _branch_files(self, branch: str, name: str) -> dict:
        """Файлы ветки из bare: git show <branch>:<path>."""
        out = {}
        for fn in ("%s/%s.cfe" % (name, name), "%s/report.md" % name,
                   "%s/report.json" % name):
            rc, data = _git(self.d, "--git-dir", str(self.bare),
                            "show", "%s:%s" % (branch, fn))
            out[fn] = data if rc == 0 else None
        return out


class TestGitPublish(GitFixture):
    def test_no_git_url_no_step(self) -> None:
        rep = renovate_ext.pipeline(self.cf1, self.cf2, work=str(self.work),
                                    run_step=self._fakes_no_recdiff(self.work))
        self.assertEqual([e["step"] for e in rep["steps"]],
                         list(renovate_ext.STEPS))
        self.assertIsNone(rep["git_publish"])

    def test_push_real_local_bare(self) -> None:
        rep = renovate_ext.pipeline(
            self.cf1, self.cf2, work=str(self.work), git_url=str(self.bare),
            run_step=self._fakes_no_recdiff(self.work))
        gp = rep["git_publish"]
        self.assertEqual(gp["status"], "ok", msg=json.dumps(gp, ensure_ascii=False))
        self.assertEqual(gp["branch"], "renovate/МсрДиф")
        self.assertTrue(gp["committed"])
        files = self._branch_files(gp["branch"], "МсрДиф")
        self.assertIsNotNone(files["МсрДиф/МсрДиф.cfe"], "cfe закоммичен")
        self.assertIn("Renovate расширений", files["МсрДиф/report.md"] or "")
        # шаг в steps и в отчёте
        self.assertIn("git_publish", [e["step"] for e in rep["steps"]])
        self.assertIn("renovate/МсрДиф", Path(rep["report_md"]).read_text(encoding="utf-8"))

    def test_custom_branch_and_artifact_copied(self) -> None:
        rep = renovate_ext.pipeline(
            self.cf1, self.cf2, work=str(self.work), git_url=str(self.bare),
            git_branch="renovate/2.5.33", run_step=self._fakes_no_recdiff(self.work))
        gp = rep["git_publish"]
        self.assertEqual(gp["status"], "ok", msg=json.dumps(gp, ensure_ascii=False))
        self.assertEqual(gp["branch"], "renovate/2.5.33")
        rc, _ = _git(self.d, "--git-dir", str(self.bare),
                     "rev-parse", "--verify", "refs/heads/renovate/2.5.33")
        self.assertEqual(rc, 0)

    def test_idempotent_second_run(self) -> None:
        for _ in range(2):
            rep = renovate_ext.pipeline(
                self.cf1, self.cf2, work=str(self.work), git_url=str(self.bare),
                run_step=self._fakes_no_recdiff(self.work))
        self.assertEqual(rep["git_publish"]["status"], "ok")
        # два коммита в ветке (первый + идемпотентный второй с новым отчётом)
        rc, out = _git(self.d, "--git-dir", str(self.bare),
                       "rev-list", "--count", "refs/heads/renovate/МсрДиф")
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(int(out.strip() or "0"), 1)

    def test_no_artifact_skipped(self) -> None:
        fakes = self._fakes_no_recdiff(self.work)
        fakes.pop("build_ext", None)   # реальный шаг: нет платформы → skipped
        fakes.pop("arbiter", None)
        with mock.patch.object(renovate_ext, "_find_1cv8", return_value=None):
            rep = renovate_ext.pipeline(self.cf1, self.cf2, work=str(self.work),
                                        git_url=str(self.bare), run_step=fakes)
        gp = rep["git_publish"]
        self.assertEqual(gp["status"], "skipped")
        self.assertIn("нет cfe-артефакта", gp["detail"])

    def test_git_error_is_failed(self) -> None:
        # несуществующий remote → clone падает → шаг failed
        rep = renovate_ext.pipeline(
            self.cf1, self.cf2, work=str(self.work),
            git_url=str(self.d / "no-such-repo.git"),
            run_step=self._fakes_no_recdiff(self.work))
        gp = rep["git_publish"]
        self.assertEqual(gp["status"], "failed")
        self.assertIn("git clone", gp["last_error"])
        # rc CLI учтёт failed-шаг
        rc = renovate_ext.main(
            [self.cf1, self.cf2, "--git-url", str(self.d / "no-such-repo.git")],
            run_step=self._fakes_no_recdiff(self.work))
        self.assertEqual(rc, 1)


class TestGitPublishMR(GitFixture):
    def test_mr_via_injection(self) -> None:
        calls = []

        def mr_fn(branch, target, result):
            calls.append((branch, target))
            return {"url": "https://gitlab.example/group/repo/-/merge_requests/1",
                    "iid": 1}

        rep = renovate_ext.pipeline(
            self.cf1, self.cf2, work=str(self.work), git_url=str(self.bare),
            mr_target="main", mr_fn=mr_fn,
            run_step=self._fakes_no_recdiff(self.work))
        self.assertEqual(rep["git_publish"]["status"], "ok")
        self.assertEqual(rep["git_publish"]["mr"]["iid"], 1)
        self.assertEqual(calls, [("renovate/МсрДиф", "main")])

    def test_mr_error_is_failed(self) -> None:
        rep = renovate_ext.pipeline(
            self.cf1, self.cf2, work=str(self.work), git_url=str(self.bare),
            mr_target="main",
            mr_fn=lambda b, t, r: {"error": "403 forbidden"},
            run_step=self._fakes_no_recdiff(self.work))
        self.assertEqual(rep["git_publish"]["status"], "failed")
        self.assertIn("403", rep["git_publish"]["last_error"])

    def test_mr_without_token_is_failed(self) -> None:
        rep = renovate_ext.pipeline(
            self.cf1, self.cf2, work=str(self.work), git_url=str(self.bare),
            mr_target="main", run_step=self._fakes_no_recdiff(self.work))
        gp = rep["git_publish"]
        self.assertEqual(gp["status"], "failed")
        self.assertIn("GITLAB_TOKEN", gp["last_error"])

    def test_stopped_pipeline_skips_git(self) -> None:
        fakes = self._fakes_no_recdiff(self.work)
        fakes["diff"] = {"rc": 1, "counts": {}, "transferable": 0,
                         "non_transferable": 0, "transfer": [], "manual": []}
        rep = renovate_ext.pipeline(self.cf1, self.cf2, work=str(self.work),
                                    git_url=str(self.bare), run_step=fakes)
        gp = rep["git_publish"]
        self.assertEqual(gp["status"], "skipped")
        self.assertIn("остановлен", gp["detail"])


if __name__ == "__main__":
    unittest.main()
