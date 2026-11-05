"""Offline tests: fake Codex, real Git. Never spend Codex tokens."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pf.implement_web import BuildError, implement, usage_from_events

VALID_HTML = ("<!doctype html><html lang='es'><head><title>Test</title>"
              "</head><body><main><h1>Working page</h1>"
              "<p>Functional landing page content.</p>"
              "</main></body></html>")


@unittest.skipUnless(shutil.which("git"), "git required")
class TestCodexImplementationGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.run_git("init")
        (self.repo / "README.md").write_text("# Project\n", encoding="utf-8")
        self.run_git("add", ".")
        self.run_git("-c", "user.name=Test",
                     "-c", "user.email=test@example.invalid",
                     "commit", "-m", "initial")

    def tearDown(self):
        self.tmp.cleanup()

    def run_git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True,
                       capture_output=True, text=True)

    def fake_codex(self, html=None, markdown=False, code=0):
        def runner(args, **kwargs):
            self.assertEqual(args[:2], ["codex", "exec"])
            self.assertIn("-C", args)
            self.assertEqual(args[args.index("-C") + 1], str(self.repo))
            self.assertIn("workspace-write", args)
            self.assertIn("--json", args)
            self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
            if html is not None:
                (self.repo / "index.html").write_text(html, encoding="utf-8")
            if markdown:
                (self.repo / "AUDIT.md").write_text("Just a report", encoding="utf-8")
            return subprocess.CompletedProcess(
                args, code,
                '{"type":"turn.completed","usage":{"input_tokens":120,"output_tokens":40}}\n',
                "simulated error" if code else "")
        return runner

    def test_real_html_required_and_token_counters_reported(self):
        result = implement(self.repo, "build the landing page",
                           runner=self.fake_codex(html=VALID_HTML))
        self.assertEqual(result["status"], "IMPLEMENTED_LOCAL")
        self.assertEqual(result["changed_files"], ["index.html"])
        self.assertEqual(result["usage"]["input_tokens"], 120)
        self.assertFalse(result["pushed"])

    def test_markdown_only_is_not_accepted(self):
        with self.assertRaisesRegex(BuildError, "No implementation"):
            implement(self.repo, "build a page",
                      runner=self.fake_codex(markdown=True))

    def test_codex_success_without_changes_is_failure(self):
        with self.assertRaisesRegex(BuildError, "No implementation"):
            implement(self.repo, "build a page", runner=self.fake_codex())

    def test_invalid_html_fails(self):
        with self.assertRaisesRegex(BuildError, "HTML (missing|structure)"):
            implement(self.repo, "build a page",
                      runner=self.fake_codex(html="<html>not finished</html>"))

    def test_nonzero_exit_fails_even_with_html(self):
        with self.assertRaisesRegex(BuildError, "Codex exited 3"):
            implement(self.repo, "build a page",
                      runner=self.fake_codex(html=VALID_HTML, code=3))

    def test_dirty_worktree_refuses_to_run(self):
        (self.repo / "notes.txt").write_text("work in progress")
        with self.assertRaisesRegex(BuildError, "not clean"):
            implement(self.repo, "build page",
                      runner=self.fake_codex(html=VALID_HTML))

    def test_path_traversal_refused(self):
        with self.assertRaisesRegex(BuildError, "must be relative"):
            implement(self.repo, "build page", "../outside.html",
                      runner=self.fake_codex(html=VALID_HTML))

    def test_timeout_refused_before_model_invocation(self):
        with self.assertRaisesRegex(BuildError, "between 30 and 1800"):
            implement(self.repo, "build page", timeout=5,
                      runner=self.fake_codex(html=VALID_HTML))

    def test_timeout_raises_failure(self):
        def stall(_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="codex", timeout=60)
        with self.assertRaisesRegex(BuildError, "timed out"):
            implement(self.repo, "build page", timeout=60, runner=stall)


class TestEventUsage(unittest.TestCase):
    def test_only_completed_turn_with_actual_usage(self):
        events = ('bad json\n'
                  '{"type":"turn.started"}\n'
                  '{"type":"turn.completed","usage":{"input_tokens":37,'
                  '"cached_input_tokens":4,"output_tokens":6}}\n')
        self.assertEqual(usage_from_events(events),
                         {"input_tokens": 37, "cached_input_tokens": 4,
                          "output_tokens": 6})


if __name__ == "__main__":
    unittest.main()
