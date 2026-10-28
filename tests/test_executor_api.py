import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pf.checkpoint import Checkpoint
from pf.executor import Executor
from pf.planner import RepoTask
from pf.audit import load_evidence
from tests.fakes import FakeGitHub, make_client

PY_FILES = {
    "requirements.txt": "flask==2.3.2\nrequests>=2.0\n",
    "main.py": "print('hi')\n",
    "tests/test_main.py": "import unittest\n",
}
FULL_BASELINE = dict(
    PY_FILES,
    **{".github/workflows/ci.yml": "name: ci\non: [push]\njobs: {}\n",
       ".github/dependabot.yml": "version: 2\nupdates: []\n",
       ".github/workflows/codeql.yml": "name: codeql\non: [push]\njobs: {}\n",
       "pyproject.toml": '[project]\nname = "x"\nversion = "0.1.0"\n'})


def task(name, **kwargs):
    return RepoTask(full_name=name, default_branch="main", **kwargs)


class ExecutorCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self._tmp.name)
        self.github = FakeGitHub()
        self.client = make_client(self.github)

    def tearDown(self):
        self._tmp.cleanup()

    def executor(self, apply=False, config=None, checkpoint=None):
        return Executor(self.client, self.workdir, mode="api",
                        apply=apply, config=config, checkpoint=checkpoint,
                        sleep_s=0, token="test-token")


class TestApiMode(ExecutorCase):
    def test_apply_python_repo_end_to_end(self):
        repo = self.github.add_repo("acme/py-a", files=dict(PY_FILES))
        initial_head = repo.head
        cp = Checkpoint(self.workdir / "state" / "checkpoint.json")
        record = self.executor(apply=True, checkpoint=cp).process_repo(
            task("acme/py-a"))
        self.assertEqual(record["decision"], "APPLIED")
        self.assertTrue(record["commit"])
        # the files really landed in the fake repo
        for expected in (".github/workflows/quality.yml",
                         ".github/dependabot.yml", "pyproject.toml",
                         ".github/workflows/codeql.yml"):
            self.assertIn(expected, repo.files)
        # history preserved: exactly one new commit, parented on the old head
        self.assertEqual(len(repo.commits), 2)
        self.assertEqual(repo.commits[-1]["parents"], [initial_head])
        # force is impossible by construction: every PATCH carries false
        self.assertEqual(len(repo.ref_patches), 1)
        self.assertIs(repo.ref_patches[0]["force"], False)
        self.assertTrue(cp.is_done("acme/py-a"))
        self.assertEqual(record["branch_protection"], False)

    def test_no_factual_change_when_baseline_complete(self):
        self.github.add_repo("acme/done", files=dict(FULL_BASELINE))
        cp = Checkpoint(self.workdir / "state" / "checkpoint.json")
        record = self.executor(apply=True, checkpoint=cp).process_repo(
            task("acme/done"))
        self.assertEqual(record["decision"], "NO_FACTUAL_CHANGE")
        self.assertIsNone(record["commit"])
        self.assertEqual(self.github.write_count, 0)
        self.assertTrue(cp.is_done("acme/done"))  # terminal success

    def test_dry_run_performs_zero_writes_and_persists_nothing(self):
        self.github.add_repo("acme/py-a", files=dict(PY_FILES))
        record = self.executor(apply=False).process_repo(task("acme/py-a"))
        self.assertEqual(record["decision"], "PLANNED")
        self.assertEqual(self.github.write_count, 0)
        reports = list((self.workdir / "reports").glob("*.json"))
        self.assertEqual(reports, [])
        self.assertFalse((self.workdir / "state" /
                          "checkpoint.json").exists())

    def test_elevated_on_protected_branch_is_recorded_not_fatal(self):
        self.github.add_repo("acme/locked", files=dict(PY_FILES),
                             protected=True)
        cp = Checkpoint(self.workdir / "state" / "checkpoint.json")
        record = self.executor(apply=True, checkpoint=cp).process_repo(
            task("acme/locked"))
        self.assertEqual(record["decision"], "ELEVATED")
        self.assertIn("protected", record["reason"])
        self.assertFalse(cp.is_done("acme/locked"))  # retried on resume

    def test_batch_reports_written_on_apply(self):
        self.github.add_repo("acme/py-a", files=dict(PY_FILES))
        executor = self.executor(apply=True)
        summary = executor.run([[task("acme/py-a")]],
                               [{"full_name": "acme/old",
                                 "decision": "SKIPPED_ARCHIVED",
                                 "reason": "archived"}])
        batch_file = self.workdir / "reports" / "api-batch-001.json"
        self.assertTrue(batch_file.exists())
        batch = json.loads(batch_file.read_text(encoding="utf-8"))
        self.assertEqual(batch["repos"][0]["decision"], "APPLIED")
        self.assertIn("branch_protection", batch["repos"][0])
        self.assertTrue((self.workdir / "reports" /
                         "run-summary.json").exists())
        self.assertEqual(summary["counts"]["APPLIED"], 1)
        self.assertEqual(summary["counts"]["SKIPPED_ARCHIVED"], 1)

    def test_own_reports_are_never_audit_evidence(self):
        self.github.add_repo("acme/py-a", files=dict(PY_FILES))
        self.executor(apply=True).run([[task("acme/py-a")]], [])
        self.assertEqual(load_evidence(self.workdir / "reports"), {})

    def test_rate_floor_stops_batch_gracefully(self):
        self.github.add_repo("acme/py-a", files=dict(PY_FILES))
        self.github.add_repo("acme/py-b", files=dict(PY_FILES))
        self.client.rate_remaining = 50  # below config floor of 100
        executor = self.executor(apply=True)
        summary = executor.run([[task("acme/py-a"), task("acme/py-b")]], [])
        self.assertEqual(summary["stop_reason"], "rate-budget-floor")
        self.assertTrue(summary["incomplete"])
        self.assertEqual(sorted(summary["pending_rate"]),
                         ["acme/py-a", "acme/py-b"])
        self.assertEqual(self.github.write_count, 0)

    def test_hook_hard_timeout_cannot_kill_batch(self):
        config = {"codex_hook": {
            "enabled": True, "timeout_s": 1,
            "command": '"%s" -c "import time; time.sleep(5)"'
                       % sys.executable}}
        self.github.add_repo("acme/py-a", files=dict(PY_FILES))
        started = time.monotonic()
        record = self.executor(apply=True, config=config).process_repo(
            task("acme/py-a"))
        elapsed = time.monotonic() - started
        self.assertEqual(record["decision"], "APPLIED")
        self.assertEqual(record["hook"]["status"], "timeout")
        self.assertLess(elapsed, 4.0)  # hard 1s timeout, not 5s sleep

    def test_hook_success_recorded_with_output_tail(self):
        config = {"codex_hook": {
            "enabled": True, "timeout_s": 60,
            "command": '"%s" -c "print(\'hook-ok\')"'
                       % sys.executable}}
        self.github.add_repo("acme/py-a", files=dict(PY_FILES))
        record = self.executor(apply=True, config=config).process_repo(
            task("acme/py-a"))
        self.assertEqual(record["hook"]["status"], "exit")
        self.assertEqual(record["hook"]["code"], 0)
        self.assertIn("hook-ok", record["hook"]["output_tail"])

    def test_disabled_hook_returns_none(self):
        self.github.add_repo("acme/py-a", files=dict(PY_FILES))
        record = self.executor(apply=True).process_repo(task("acme/py-a"))
        self.assertIsNone(record["hook"])

    def test_missing_branch_recorded_cleanly(self):
        self.github.add_repo("acme/py-a", files=dict(PY_FILES),
                             default_branch="trunk")
        record = self.executor(apply=True).process_repo(task("acme/py-a"))
        self.assertEqual(record["decision"], "FAILED_REF")


if __name__ == "__main__":
    unittest.main()
