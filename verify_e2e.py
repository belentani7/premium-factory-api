"""End-to-end verification harness (offline).

Runs the real CLI over a fake GitHub + a real local git remote:
  inventory -> plan -> dry-run (0 writes) -> apply -> resume -> status,
then validates every generated workflow YAML (BaseLoader keeps `on`
literal) and exercises clone mode end-to-end with real git history.

Passes the token in the args (the harness learned that the hard way).
Prints VERIFICATION OK and exits 0 only if every stage holds.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pf import cli, ghapi  # noqa: E402
from pf.checkpoint import Checkpoint  # noqa: E402
from pf.executor import Executor  # noqa: E402
from pf.planner import RepoTask  # noqa: E402
from tests.fakes import FakeGitHub  # noqa: E402
from tests.test_executor_api import PY_FILES  # noqa: E402

STAGES = []


def stage(name):
    def wrap(fn):
        STAGES.append((name, fn))
        return fn
    return wrap


def build_org():
    github = FakeGitHub(login="factory-owner")
    full_baseline = dict(
        PY_FILES,
        **{".github/workflows/ci.yml": "name: ci\non: [push]\njobs: {}\n",
           ".github/dependabot.yml": "version: 2\nupdates: []\n",
           ".github/workflows/codeql.yml": "name: codeql\non: push\njobs: {}",
           "pyproject.toml": '[project]\nname = "x"\nversion = "0.1.0"\n'})
    github.add_repo("acme/py-legacy", files=dict(PY_FILES))
    github.add_repo("acme/node-app", files={
        "package.json": json.dumps({"name": "node-app"}),
        "index.js": "console.log(1)\n",
        "tests/app.test.js": "test('x', () => {})\n"})
    github.add_repo("acme/rust-cli", files={
        "Cargo.toml": "[package]\nname = \"cli\"\nversion = \"0.1.0\"\n",
        "Cargo.lock": "# lock\n", "src/main.rs": "fn main() {}\n",
        "tests/cli.rs": "#[test]\nfn t() {}\n"})
    github.add_repo("acme/go-svc", files={
        "go.mod": "module example.com/svc\n\ngo 1.21\n",
        "main.go": "package main\n", "main_test.go": "package main\n"})
    github.add_repo("acme/node-bare", files={
        "package.json": json.dumps({"name": "bare",
                                    "scripts": {"build": "esbuild"}}),
        "index.js": "1\n"})
    github.add_repo("acme/data-only", files={"data.json": "{}\n"})
    github.add_repo("acme/done-repo", files=dict(full_baseline))
    github.add_repo("acme/locked", files=dict(PY_FILES), protected=True)
    github.add_repo("acme/archived-one", files=dict(PY_FILES),
                    archived=True)
    github.add_repo("acme/forked", files=dict(PY_FILES), fork=True)
    return github


def popen(args, wd):
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(args))
    return code, out.getvalue()


def cli_args(wd, *extra):
    # the harness must pass the token in the args (learned the hard way)
    return ["--token", "t", "--api-url", "https://api.github.test",
            "--workdir", str(wd)] + list(extra)


@stage("inventory")
def stage_inventory(github, wd):
    code, out = popen(cli_args(wd, "inventory"), wd)
    inv = json.loads((wd / "state" / "inventory.json").read_text(
        encoding="utf-8"))
    assert code == 0, out
    assert inv["total"] == 10, inv["total"]
    assert inv["login"] == "factory-owner"
    print("  inventory: total=%d archived=%d fork=%d" % (
        inv["total"],
        sum(1 for r in inv["repos"] if r["archived"]),
        sum(1 for r in inv["repos"] if r["fork"])))


@stage("plan")
def stage_plan(github, wd):
    code, out = popen(cli_args(wd, "plan"), wd)
    assert code == 0, out
    assert "batch 001 (8)" in out, out  # 10 - archived - fork = 8 pending
    assert "SKIPPED_ARCHIVED (1)" in out and "SKIPPED_FORK (1)" in out
    print("  plan: 8 repos in 1 batch of <=8, 2 pre-skipped")


@stage("dry-run")
def stage_dryrun(github, wd):
    writes_before = github.write_count
    code, out = popen(cli_args(wd, "run", "--sleep", "0"), wd)
    assert code == 0, out
    assert github.write_count == writes_before, "dry-run wrote to GitHub!"
    assert not (wd / "state" / "checkpoint.json").exists(), \
        "dry-run polluted the checkpoint (the old PLANNED_DRY_RUN bug)"
    assert not list((wd / "reports").glob("*-batch-*.json"))
    planned = out.count("PLANNED") + out.count("planned")
    print("  dry-run: 0 writes, 0 checkpoint entries, %d repos previewed"
          % planned)


@stage("apply")
def stage_apply(github, wd):
    code, out = popen(cli_args(wd, "run", "--apply", "--sleep", "0"), wd)
    assert code == 1, "elevated repo must surface as exit 1: %s" % out
    summary = json.loads((wd / "reports" / "run-summary.json").read_text(
        encoding="utf-8"))
    counts = summary["counts"]
    assert counts.get("APPLIED") == 5, counts
    assert counts.get("NO_FACTUAL_CHANGE") == 2, counts  # done-repo + data-only
    assert counts.get("ELEVATED") == 1, counts
    assert counts.get("SKIPPED_ARCHIVED") == 1, counts
    assert counts.get("SKIPPED_FORK") == 1, counts
    repo = github.repos["acme/py-legacy"]
    assert len(repo.commits) == 2 and repo.commits[1]["parents"] == [
        repo.commits[0]["sha"]], "history must stay linear"
    assert all(p["force"] is False for p in repo.ref_patches)
    assert ".github/workflows/quality.yml" in repo.files
    node = github.repos["acme/node-app"]
    patched = json.loads(node.files["package.json"])
    assert patched["scripts"]["test"] == "node --test"
    assert "npm test" in node.files[".github/workflows/quality.yml"]
    batch = json.loads((wd / "reports" / "api-batch-001.json").read_text(
        encoding="utf-8"))
    assert all("branch_protection" in r for r in batch["repos"])
    print("  apply: APPLIED=5 NO_FACTUAL_CHANGE=2 ELEVATED=1 exit=1; "
          "linear history, force=false, node workflow from patched json")


@stage("yaml-validity")
def stage_yaml(github, wd):
    try:
        import yaml
    except ImportError:
        print("  yaml: PyYAML not installed -> skipped (package stays "
              "stdlib-only)")
        return
    checked = 0
    for path in sorted((wd / "reports").rglob("*-batch-*.json")):
        batch = json.loads(path.read_text(encoding="utf-8"))
        for repo_record in batch["repos"]:
            full_name = repo_record["full_name"]
            files = github.repos[full_name].files
            for name in files:
                if name.endswith((".yml", ".yaml")):
                    data = yaml.load(files[name], Loader=yaml.BaseLoader)
                    assert isinstance(data, dict), name
                    if name.startswith(".github/workflows/"):
                        assert "on" in data, (
                            "PyYAML 1.1 quirk leaked: %s in %s"
                            % (name, full_name))
                    checked += 1
    assert checked >= 6, checked
    print("  yaml: %d generated workflow(s) parse with literal `on`"
          % checked)


@stage("resume")
def stage_resume(github, wd):
    # a session that died mid-batch: one terminal entry lost, and the
    # ELEVATED repo gets its protection lifted -> both retried on resume
    cp_path = wd / "state" / "checkpoint.json"
    cp = Checkpoint.load(cp_path)
    del cp.entries["acme/py-legacy"]
    cp.meta["resumed"] = True
    cp._flush()
    github.repos["acme/locked"].protected = False
    writes_before = github.write_count
    legacy_before = len(github.repos["acme/py-legacy"].commits)
    locked_before = len(github.repos["acme/locked"].commits)
    code, out = popen(cli_args(wd, "run", "--apply", "--sleep", "0"), wd)
    assert code == 0, out
    assert github.write_count > writes_before, "resume did nothing"
    # the retried ELEVATED repo is now really applied (commit +1)
    assert len(github.repos["acme/locked"].commits) == locked_before + 1
    # idempotency: the re-processed legacy repo finds everything present,
    # records NO_FACTUAL_CHANGE and adds ZERO commits
    assert len(github.repos["acme/py-legacy"].commits) == legacy_before
    summary = json.loads((wd / "reports" / "run-summary.json").read_text(
        encoding="utf-8"))
    counts = summary["counts"]
    assert counts.get("APPLIED") == 1, counts
    assert counts.get("NO_FACTUAL_CHANGE") == 1, counts
    assert counts.get("ALREADY_APPLIED") == 6, counts
    print("  resume: interrupted batch recovered; locked repo applied on "
          "retry, legacy repo idempotent (0 extra commits)")


@stage("status")
def stage_status(github, wd):
    code, out = popen(cli_args(wd, "status"), wd)
    assert code == 0, out
    assert "APPLIED" in out and "NO_FACTUAL_CHANGE" in out
    assert "checkpoint: 8 entr" in out
    assert "retry-on-resume" not in out  # everything terminal after resume
    print("  status: 8 entries, all terminal, retry list empty")


@stage("clone-mode")
def stage_clone(github, wd):
    if not shutil.which("git"):
        print("  clone: git missing -> skipped")
        return
    base = Path(tempfile.mkdtemp(prefix="pf-clone-"))
    try:
        origin = base / "origin.git"
        seed = base / "seed"
        run_git("init", "--bare", "-b", "main", str(origin))
        seed.mkdir()
        run_git("init", "-b", "main", str(seed))
        for name, content in PY_FILES.items():
            target = seed / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        run_git("add", ".", cwd=str(seed))
        run_git("-c", "user.name=seed", "-c", "user.email=s@t", "commit",
                "-m", "seed commit", cwd=str(seed))
        run_git("push", str(origin), "main", cwd=str(seed))
        executor = Executor(None, wd / "clone-work", mode="clone",
                            apply=True, config={"sleep_s": 0})
        record = executor.process_repo(
            RepoTask(full_name="local/py-real", default_branch="main"),
            remote=str(origin))
        assert record["decision"] == "APPLIED", record
        log = run_git("--git-dir", str(origin), "log", "--format=%s",
                      "main").stdout.strip().splitlines()
        assert len(log) == 2 and log[1] == "seed commit", log
        assert "chore(quality)" in log[0]
        push_argv = Executor.build_push_cmd("main")
        assert "--force" not in push_argv and "-f" not in push_argv
        print("  clone: real git push to main, history preserved "
              "(2 commits), no force flags")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@stage("no-markdown")
def stage_no_markdown(github, wd):
    offenders = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                 if p.suffix == ".md"]
    assert not offenders, offenders
    print("  no-markdown: 0 .md files in the package")


def run_git(*args, cwd=None):
    proc = subprocess.run(["git"] + list(args), cwd=cwd,
                          capture_output=True, text=True)
    assert proc.returncode == 0, (args, proc.stderr)
    return proc


def main():
    os.environ.pop("GITHUB_TOKEN", None)
    original_transport = ghapi._urllib_transport
    github = build_org()
    ghapi._urllib_transport = github.transport
    wd = ROOT / "e2e-work"
    try:
        if wd.exists():
            shutil.rmtree(wd)
        wd.mkdir()
        for name, fn in STAGES:
            fn(github, wd)
        print("VERIFICATION OK (%d stages)" % len(STAGES))
        return 0
    finally:
        ghapi._urllib_transport = original_transport
        shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
