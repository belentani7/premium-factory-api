import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pf import cli, ghapi
from pf.checkpoint import Checkpoint
from tests.fakes import FakeGitHub
from tests.test_executor_api import PY_FILES

TOKEN_ENV = "GITHUB_TOKEN"


class CliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self._tmp.name)
        self.github = FakeGitHub()
        self._original_transport = ghapi._urllib_transport
        ghapi._urllib_transport = self.github.transport
        self._original_token = os.environ.pop(TOKEN_ENV, None)

    def tearDown(self):
        ghapi._urllib_transport = self._original_transport
        if self._original_token is not None:
            os.environ[TOKEN_ENV] = self._original_token
        self._tmp.cleanup()

    def popen(self, *args):
        """Run the CLI, return (exit_code, stdout). run stays instant."""
        if "run" in args and "--sleep" not in args:
            args = list(args) + ["--sleep", "0"]
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = cli.main(list(args))
        except SystemExit as exc:  # argparse errors
            return exc.code or 2, out.getvalue()
        return code, out.getvalue()

    def add_repos(self, count, prefix="acme/r%02d"):
        for i in range(count):
            self.github.add_repo(prefix % i, files=dict(PY_FILES))

    def base(self, *extra):
        return ["--token", "t", "--api-url", "https://api.github.test",
                "--workdir", str(self.wd)] + list(extra)


class TestGlobalFlagsBothPositions(CliCase):
    def test_flags_before_subcommand(self):
        code, out = self.popen(*self.base("inventory"))
        self.assertEqual(code, 0, out)
        self.assertTrue((self.wd / "state" /
                         "inventory.json").exists())

    def test_flags_after_subcommand(self):
        code, out = self.popen("inventory", "--token", "t", "--api-url",
                               "https://api.github.test", "--workdir",
                               str(self.wd))
        self.assertEqual(code, 0, out)
        self.assertTrue((self.wd / "state" /
                         "inventory.json").exists())

    def test_after_position_overrides_before_position(self):
        wd2 = self.wd / "elsewhere"
        code, _ = self.popen(*self.base("--workdir", str(wd2), "inventory"))
        self.assertEqual(code, 0)
        self.assertTrue((wd2 / "state" / "inventory.json").exists())


class TestRunFlow(CliCase):
    def test_dry_run_then_apply_is_never_blocked(self):
        self.add_repos(2)
        code, out = self.popen(*self.base("run"))
        self.assertEqual(code, 0, out)
        self.assertFalse((self.wd / "state" /
                          "checkpoint.json").exists())  # dry-run writes none
        self.assertNotIn("PLANNED_DRY_RUN", out)
        code, out = self.popen(*self.base("run", "--apply"))
        self.assertEqual(code, 0, out)
        cp = Checkpoint.load(self.wd / "state" / "checkpoint.json")
        self.assertEqual(cp.counts().get("APPLIED"), 2)
        self.assertTrue((self.wd / "reports" /
                         "api-batch-001.json").exists())

    def test_status_reads_checkpoint(self):
        self.add_repos(1)
        self.popen(*self.base("run", "--apply"))
        code, out = self.popen(*self.base("status"))
        self.assertEqual(code, 0, out)
        self.assertIn("APPLIED", out)

    def test_resume_skips_applied_and_writes_nothing_new(self):
        self.add_repos(3)
        self.popen(*self.base("run", "--apply", "--limit", "2"))
        writes_after_first = self.github.write_count
        code, out = self.popen(*self.base("run", "--apply"))
        self.assertEqual(code, 0, out)
        self.assertGreater(self.github.write_count, writes_after_first)
        cp = Checkpoint.load(self.wd / "state" / "checkpoint.json")
        self.assertEqual(len(cp.entries), 3)
        writes_after_resume = self.github.write_count
        code, out = self.popen(*self.base("run", "--apply"))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.github.write_count, writes_after_resume)
        # the resumed run touched nothing: everything already terminal
        summary = json.loads((self.wd / "reports" /
                              "run-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["counts"].get("APPLIED", 0), 0)
        self.assertEqual(summary["counts"].get("ALREADY_APPLIED"), 3)

    def test_pilot_limit_8_of_12(self):
        self.add_repos(12)
        code, out = self.popen(*self.base("run", "--apply", "--limit", "8"))
        self.assertEqual(code, 0, out)
        cp = Checkpoint.load(self.wd / "state" / "checkpoint.json")
        self.assertEqual(len(cp.entries), 8)
        summary = json.loads((self.wd / "reports" /
                              "run-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["counts"].get("APPLIED"), 8)

    def test_codex_audit_becomes_repo_evidence(self):
        self.add_repos(1, "acme/solo%02d")
        audit_dir = self.wd / "reports"
        audit_dir.mkdir(exist_ok=True)
        (audit_dir / "codex-batch-001.json").write_text(json.dumps(
            {"repos": [{"repo": "acme/solo00",
                        "findings": ["no CI workflow"]}]},
        ), encoding="utf-8")
        code, out = self.popen(*self.base("run", "--apply"))
        self.assertEqual(code, 0, out)
        batch = json.loads((self.wd / "reports" / "api-batch-001.json")
                           .read_text(encoding="utf-8"))
        evidence = batch["repos"][0]["evidence"]
        self.assertEqual(evidence[0]["file"], "codex-batch-001.json")
        self.assertEqual(evidence[0]["findings"], ["no CI workflow"])

    def test_elevated_repo_makes_apply_exit_1(self):
        self.github.add_repo("acme/locked", files=dict(PY_FILES),
                             protected=True)
        self.github.add_repo("acme/open", files=dict(PY_FILES))
        code, out = self.popen(*self.base("run", "--apply"))
        self.assertEqual(code, 1)
        self.assertIn("ELEVATED", out)

    def test_missing_token_is_a_clean_error(self):
        code, out = self.popen("run", "--workdir", str(self.wd))
        self.assertEqual(code, 2)
        self.assertIn("GITHUB_TOKEN", out)


if __name__ == "__main__":
    unittest.main()
