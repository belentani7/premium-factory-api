import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pf.executor import Executor
from pf.planner import RepoTask
from tests.test_executor_api import PY_FILES

GIT = shutil.which("git")


def git(*args, cwd=None, check=True):
    proc = subprocess.run(["git"] + list(args), cwd=cwd,
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError("git %s failed: %s" % (args, proc.stderr))
    return proc


def make_origin(base, name, files):
    """A real local bare remote with one seed commit on main."""
    origin = base / name
    seed = base / (name + "-seed")
    git("init", "--bare", "-b", "main", str(origin))
    seed.mkdir(parents=True)
    git("init", "-b", "main", str(seed))
    for path, content in files.items():
        target = seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    git("add", ".", cwd=str(seed))
    git("-c", "user.name=seed", "-c", "user.email=seed@test",
        "commit", "-m", "seed commit", cwd=str(seed))
    git("push", str(origin), "main", cwd=str(seed))
    return origin


def origin_log(origin):
    proc = git("--git-dir", str(origin), "log", "--format=%s", "main")
    return proc.stdout.strip().splitlines()


def origin_head(origin):
    return git("--git-dir", str(origin), "rev-parse", "main").stdout.strip()


def task(name):
    return RepoTask(full_name=name, default_branch="main",
                    clone_url="https://github.com/%s.git" % name)


@unittest.skipUnless(GIT, "git is required for clone-mode tests")
class TestCloneMode(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.workdir = self.base / "work"
        self.workdir.mkdir()
        self.executor = Executor(None, self.workdir, mode="clone",
                                 apply=True, config={}, sleep_s=0,
                                 token=None)

    def tearDown(self):
        self._tmp.cleanup()

    def test_push_to_main_preserves_history_without_force(self):
        origin = make_origin(self.base, "py-origin", dict(PY_FILES))
        before = origin_log(origin)
        record = self.executor.process_repo(task("acme/py-a"),
                                            remote=str(origin))
        self.assertEqual(record["decision"], "APPLIED", record["reason"])
        self.assertTrue(record["commit"])
        after = origin_log(origin)
        self.assertEqual(len(after), len(before) + 1)  # additive only
        self.assertEqual(after[1:], before)            # history intact
        self.assertIn("chore(quality)", after[0])
        # no force flag exists anywhere in the push command
        push_argv = Executor.build_push_cmd("main")
        self.assertEqual(push_argv, ["push", "origin", "main"])
        self.assertNotIn("--force", push_argv)
        self.assertNotIn("-f", push_argv)
        # tmp clone cleaned up after success
        self.assertEqual(list((self.workdir / "clones").glob("*")), [])

    def test_no_factual_change_touches_nothing(self):
        full = dict(PY_FILES,
                    **{".github/workflows/ci.yml": "name: ci\non: push\n",
                       ".github/dependabot.yml": "version: 2\nupdates: []\n",
                       ".github/workflows/codeql.yml": "name: q\non: push\n",
                       "pyproject.toml": '[project]\nname="x"\n'})
        origin = make_origin(self.base, "done-origin", full)
        before = origin_head(origin)
        record = self.executor.process_repo(task("acme/done"),
                                            remote=str(origin))
        self.assertEqual(record["decision"], "NO_FACTUAL_CHANGE")
        self.assertEqual(origin_head(origin), before)

    def test_dry_run_writes_nothing_to_origin(self):
        origin = make_origin(self.base, "dry-origin", dict(PY_FILES))
        before = origin_head(origin)
        dry = Executor(None, self.workdir, mode="clone", apply=False,
                       config={}, sleep_s=0, token=None)
        record = dry.process_repo(task("acme/py-a"), remote=str(origin))
        self.assertEqual(record["decision"], "PLANNED")
        self.assertEqual(origin_head(origin), before)
        self.assertEqual(list((self.workdir / "clones").glob("*")), [])

    def test_protected_branch_push_is_elevated_not_fatal(self):
        origin = make_origin(self.base, "lock-origin", dict(PY_FILES))
        hook = origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8",
                        newline="\n")
        os.chmod(hook, 0o755)
        record = self.executor.process_repo(task("acme/locked"),
                                            remote=str(origin))
        self.assertEqual(record["decision"], "ELEVATED", record["reason"])
        self.assertIn("rejected", record["reason"])

    def test_clone_failure_recorded_cleanly(self):
        record = self.executor.process_repo(
            task("acme/ghost"),
            remote=str(self.base / "does-not-exist.git"))
        self.assertEqual(record["decision"], "FAILED_CLONE")


if __name__ == "__main__":
    unittest.main()
