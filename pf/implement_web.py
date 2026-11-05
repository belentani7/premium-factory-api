"""One-repository Codex implementation gate: real files, not Markdown reports.

Run locally: python -m pf.implement_web --repo PATH --task "..."
No remote push, no commit, no network or GitHub token is needed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


class BuildError(Exception):
    """An implementation did not meet its observable acceptance gate."""


def git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=30)
    if proc.returncode:
        raise BuildError("git failed: " + (proc.stderr or "").strip()[-500:])
    return proc.stdout.strip()


def changed_files(repo):
    """Include tracked, staged and untracked files; exclude ignored files."""
    tracked = git(repo, "diff", "--name-only", "HEAD", "--").splitlines()
    untracked = git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted(set(tracked + untracked))


def usage_from_events(text):
    """Only report counters Codex actually emitted; never estimate usage."""
    found = {}
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                found = {key: val for key, val in usage.items()
                         if isinstance(val, (int, float)) and not isinstance(val, bool)}
    return found


def validate_html(path):
    if not path.is_file() or path.stat().st_size < 80:
        raise BuildError("HTML missing or too small: " + str(path))
    content = path.read_text(encoding="utf-8", errors="replace")
    for pattern in (r"<!doctype\s+html", r"<html\b", r"<body\b",
                    r"</body\s*>", r"</html\s*>"):
        if re.search(pattern, content, re.IGNORECASE) is None:
            raise BuildError("HTML structure incomplete (" + pattern + "): " +
                             str(path))


def implement(repo, task, required_file="index.html", timeout=360,
              effort="medium", verify_command=None, runner=None):
    repo = Path(repo).expanduser().resolve()
    expected = Path(required_file)
    if expected.is_absolute() or ".." in expected.parts or not expected.parts:
        raise BuildError("--required-file must be relative inside the repository")
    if not task.strip():
        raise BuildError("--task cannot be empty")
    if timeout < 30 or timeout > 1800:
        raise BuildError("--timeout must be between 30 and 1800 seconds")
    if effort not in ("low", "medium", "high"):
        raise BuildError("effort must be low, medium or high")

    top = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
    if top != repo:
        raise BuildError("--repo must point to the repository root: " + str(top))
    if git(repo, "status", "--porcelain"):
        raise BuildError("Working tree is not clean. Commit or stash existing changes first.")

    target = (repo / expected).resolve()
    if not target.is_relative_to(repo):
        raise BuildError("--required-file escapes the repository")
    prompt = (
        "You are an implementation agent, not a reviewer. Work only in this "
        "checked-out repository and implement the task NOW.\n"
        "TASK: " + task.strip() + "\n"
        "MANDATORY DELIVERABLE: create or materially change the real source "
        "file " + expected.as_posix() + ". Build an accessible, responsive, "
        "functional interface matching the existing stack. Preserve existing "
        "behavior and use local assets where appropriate. Do not merely "
        "describe changes, create a plan, or output Markdown instead of code. "
        "Do not commit, push, alter credentials or perform destructive actions. "
        "Run focused local checks if possible. Finish with a short description "
        "of modified files and checks; the actual files are the deliverable."
    )

    if runner is None:
        runner = subprocess.run
    with tempfile.TemporaryDirectory(prefix="codex-web-") as tmp:
        final_message = Path(tmp) / "last-message.txt"
        cmd = ["codex", "exec", "-C", str(repo), "-s", "workspace-write",
               "-c", 'model_reasoning_effort="' + effort + '"',
               "--json", "-o", str(final_message), prompt]
        # A coding task on local sources should not expose a destructive
        # GitHub token to the subprocess. No network access is enabled.
        env = dict(os.environ)
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_TOKEN", None)
        try:
            result = runner(cmd, cwd=str(repo), env=env,
                            capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise BuildError("Codex timed out after " + str(timeout) +
                             " seconds; inspect git diff before retrying") from exc
        except FileNotFoundError as exc:
            raise BuildError("Codex CLI not found on PATH") from exc

        usage = usage_from_events(result.stdout or "")
        modified = changed_files(repo)
        if result.returncode:
            raise BuildError("Codex exited " + str(result.returncode) +
                             "; files=" + repr(modified) + "; " +
                             (result.stderr or "")[-500:])
        if expected.as_posix() not in modified:
            raise BuildError("No implementation: required file unchanged (" +
                             expected.as_posix() + "); changed=" + repr(modified))
        if not target.is_file():
            raise BuildError("Required source file is missing: " +
                             expected.as_posix())
        if target.suffix.lower() == ".html":
            validate_html(target)
        if verify_command:
            args = shlex.split(verify_command, posix=(os.name != "nt"))
            if not args:
                raise BuildError("Empty verification command")
            check = subprocess.run(args, cwd=str(repo), capture_output=True,
                                   text=True, timeout=min(timeout, 300))
            if check.returncode:
                raise BuildError("Verification failed (exit " +
                                 str(check.returncode) + "): " +
                                 (check.stdout + check.stderr)[-1000:])

        return {"status": "IMPLEMENTED_LOCAL", "repo": str(repo),
                "required_file": expected.as_posix(),
                "changed_files": modified, "usage": usage,
                "verification": "passed" if verify_command else "HTML structure only"
                if target.suffix.lower() == ".html" else "file change only",
                "committed": False, "pushed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Use Codex to implement a web page; fail if no real code changed.")
    parser.add_argument("--repo", required=True, help="clean, locally cloned Git repo")
    parser.add_argument("--task", required=True, help="one concrete implementation task")
    parser.add_argument("--required-file", default="index.html",
                        help="source file Codex MUST create/change (default: index.html)")
    parser.add_argument("--timeout", type=int, default=360,
                        help="max execution seconds, 30-1800 (default 360)")
    parser.add_argument("--effort", choices=["low", "medium", "high"],
                        default="medium")
    parser.add_argument("--verify-command",
                        help="optional local test command, e.g. 'npm test'")
    args = parser.parse_args(argv)
    try:
        result = implement(args.repo, args.task, args.required_file,
                           args.timeout, args.effort, args.verify_command)
    except (BuildError, subprocess.TimeoutExpired) as exc:
        print("IMPLEMENTATION FAILED: " + str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
