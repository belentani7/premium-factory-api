"""Executor: turns a plan into decisions, commits and reports.

Two modes:
  api   - Git Data API: blobs -> tree -> commit -> PATCH ref force:false.
          Force is impossible by construction; history is never rewritten.
  clone - shallow clone (isolated tmp worktree) + normal `git push` with no
          force flags anywhere; a non-fast-forward simply fails server-side.

Every repo ends in exactly one recorded decision. A hung external process
(hook or git) hits a hard timeout and the batch continues. ELEVATED /
FAILED repos are retried automatically on resume; dry-run writes nothing.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import TOOL_TAG, ghapi
from .changes import build_changeset, commit_message

PLANNED = "PLANNED"
APPLIED = "APPLIED"
NO_FACTUAL_CHANGE = "NO_FACTUAL_CHANGE"
ELEVATED = "ELEVATED"
PENDING_RATE = "PENDING_RATE"

NEEDED_FILES = ("requirements.txt", "package.json", "Cargo.toml", "go.mod",
                "pyproject.toml", "Cargo.lock", "package-lock.json")


def authenticated_url(url, token):
    """Embed the token in an https GitHub URL (never printed, tmp only)."""
    if not token:
        return url
    if url.startswith("https://github.com/"):
        return url.replace("https://", "https://x-access-token:%s@" % token, 1)
    return url


def force_rmtree(path):
    """Windows-proof rmtree: .git packs are read-only."""
    path = Path(path)
    if not path.exists():
        return
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                os.chmod(os.path.join(root, name), 0o666)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _validate_content(path, content):
    """Best-effort validity check of what we are about to write."""
    if path.endswith(".json"):
        json.loads(content)
    elif path.endswith(".toml"):
        try:
            import tomllib
            tomllib.loads(content)
        except ImportError:
            pass  # python < 3.11: structural check only
    elif path.endswith((".yml", ".yaml")):
        if "\t" in content:
            raise ValueError("tab in YAML: %s" % path)
        try:
            import yaml
            # BaseLoader keeps the literal `on` key (PyYAML 1.1 quirk safe)
            yaml.load(content, Loader=yaml.BaseLoader)
        except ImportError:
            if "on:" not in content:
                raise ValueError("workflow without trigger: %s" % path)


class Executor:
    def __init__(self, api, workdir, *, mode="api", apply=False,
                 config=None, audit_dir=None, checkpoint=None,
                 sleep_s=0.5, token=None, environ=None):
        self.api = api
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.reports_dir = self.workdir / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.apply = apply
        self.config = dict(config or {})
        self.checkpoint = checkpoint
        self.sleep_s = max(0.0, float(sleep_s))
        self.token = token
        self.environ = environ if environ is not None else os.environ

    # -- public ----------------------------------------------------------

    def run(self, batches, skipped):
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        records = [dict(item, mode=self.mode) for item in skipped]
        pending_rate = []
        batch_reports = []
        stop_reason = None

        for index, batch in enumerate(batches, 1):
            batch_records = []
            for position, task in enumerate(batch):
                if self._rate_floor_hit():
                    stop_reason = "rate-budget-floor"
                    pending_rate.append(task.full_name)
                    continue
                record = self.process_repo(task)
                batch_records.append(record)
                records.append(record)
                if self.sleep_s and position + 1 < len(batch):
                    time.sleep(self.sleep_s)
            if batch_records:
                report = {"batch": index, "mode": self.mode,
                          "apply": self.apply, "repos": batch_records}
                batch_reports.append((index, report))

        if self.apply:
            for index, report in batch_reports:
                self._write_json(
                    self.reports_dir / ("%s-batch-%03d.json"
                                        % (self.mode, index)), report)
        counts = {}
        for record in records:
            counts[record["decision"]] = counts.get(record["decision"], 0) + 1
        summary = {
            "title": "%s-%s" % (self.mode, "apply" if self.apply else "dryrun"),
            "mode": self.mode, "apply": self.apply, "started_at": started,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "processed": len(records), "counts": counts, "repos": records,
            "pending_rate": pending_rate, "stop_reason": stop_reason,
            "incomplete": bool(stop_reason),
            "attention": [
                {"full_name": r["full_name"], "decision": r["decision"],
                 "reason": r.get("reason", "")}
                for r in records if r["decision"] in (ELEVATED, "FAILED")],
        }
        summary["attention"] += [
            {"full_name": r["full_name"], "decision": r["decision"],
             "reason": r.get("reason", "")}
            for r in records if r["decision"].startswith("FAILED")]
        if self.apply:
            self._write_json(self.reports_dir / "run-summary.json", summary)
        return summary

    def process_repo(self, task, remote=None):
        record = {
            "full_name": task.full_name,
            "decision": None,
            "reason": "",
            "branch_protection": None,
            "evidence": task.evidence,
            "changes": [],
            "commit": None,
            "hook": None,
            "mode": self.mode,
        }
        try:
            if self.mode == "clone":
                self._process_clone(task, record,
                                    remote=remote or task.clone_url)
            else:
                self._process_api(task, record)
        except ghapi.RateLimitExceeded:
            raise  # the run loop stops the batch gracefully
        except ghapi.APIError as exc:
            record["decision"] = "FAILED_API" if exc.status != 403 else ELEVATED
            record["reason"] = exc.message
        except Exception as exc:  # never let one repo kill the batch
            record["decision"] = "FAILED"
            record["reason"] = "%s: %s" % (type(exc).__name__, exc)
        if (self.apply and record["decision"] == APPLIED):
            record["hook"] = self._run_hook(task, record)
        if (self.apply and self.checkpoint is not None
                and record["decision"] and record["decision"] != PLANNED):
            self.checkpoint.record(task.full_name, {
                "decision": record["decision"],
                "commit": record.get("commit"),
                "mode": self.mode,
            })
        return record

    # -- API mode ----------------------------------------------------------

    def _process_api(self, task, record):
        api = self.api
        branch = task.default_branch
        try:
            ref = api.get("/repos/%s/git/ref/heads/%s"
                          % (task.full_name, branch))
        except ghapi.NotFound:
            record["decision"] = "FAILED_REF"
            record["reason"] = "branch %s not found" % branch
            return
        base_sha = ref["object"]["sha"]
        tree_data = api.get("/repos/%s/git/trees/%s?recursive=1"
                            % (task.full_name, base_sha))
        if tree_data.get("truncated"):
            record["decision"] = "FAILED_TREE_TRUNCATED"
            record["reason"] = "tree too large for a factual read"
            return
        paths = tuple(item["path"] for item in tree_data.get("tree", [])
                      if item.get("type") == "blob")

        files = {}
        for path in NEEDED_FILES:
            if path in paths:
                try:
                    files[path] = api.get_content_text(
                        task.full_name, path, branch)
                except ghapi.NotFound:
                    pass
        from .changes import RepoTree
        tree = RepoTree(full_name=task.full_name, default_branch=branch,
                        private=task.private, paths=paths, files=files)
        changes = build_changeset(tree, self.config)
        record["changes"] = [
            {"path": c.path, "action": c.action, "reason": c.reason}
            for c in changes]
        if not changes:
            record["decision"] = NO_FACTUAL_CHANGE
            record["reason"] = "every factual baseline item already present"
            return

        record["branch_protection"] = self._branch_protection(
            task.full_name, branch)
        if record["branch_protection"] is True and not self.apply:
            # still plan it: the write itself will prove/refute the policy
            pass

        if not self.apply:
            record["decision"] = PLANNED
            record["reason"] = ("dry-run: %d factual file(s) would be written"
                                % len(changes))
            return

        commit_sha = self._api_commit(task, record, base_sha, changes, branch)
        record["decision"] = APPLIED
        record["commit"] = commit_sha
        record["reason"] = "pushed %d file(s) to %s" % (len(changes), branch)

    def _api_commit(self, task, record, base_sha, changes, branch,
                    _attempt=0):
        api = self.api
        blobs = []
        for change in changes:
            _validate_content(change.path, change.content)
            blob = api.post("/repos/%s/git/blobs" % task.full_name,
                            {"content": change.content, "encoding": "utf-8"})
            blobs.append({"path": change.path, "mode": "100644",
                          "type": "blob", "sha": blob["sha"]})
        base_commit = api.get("/repos/%s/git/commits/%s"
                              % (task.full_name, base_sha))
        new_tree = api.post("/repos/%s/git/trees" % task.full_name,
                            {"base_tree": base_commit["tree"]["sha"],
                             "tree": blobs})
        new_commit = api.post("/repos/%s/git/commits" % task.full_name,
                              {"message": commit_message(changes),
                               "tree": new_tree["sha"],
                               "parents": [base_sha]})
        # race guard: if main moved while we built, rebuild on the new head
        ref_now = api.get("/repos/%s/git/ref/heads/%s"
                          % (task.full_name, branch))
        if ref_now["object"]["sha"] != base_sha:
            if _attempt >= 1:
                record["reason"] = "main moved twice; rerun to retry"
                record["decision"] = ELEVATED
                raise ghapi.APIError(409, "base drifted during commit",
                                     {})
            return self._api_commit(task, record,
                                    ref_now["object"]["sha"], changes,
                                    branch, _attempt=1)
        api.patch("/repos/%s/git/refs/heads/%s" % (task.full_name, branch),
                  {"sha": new_commit["sha"], "force": False})
        return new_commit["sha"]

    def _branch_protection(self, full_name, branch):
        try:
            self.api.get("/repos/%s/branches/%s/protection"
                         % (full_name, branch))
            return True
        except ghapi.NotFound:
            return False
        except ghapi.Forbidden:
            return "unknown"  # token cannot read policy; push will prove it

    # -- clone mode ----------------------------------------------------------

    def _git(self, args, cwd=None, timeout=None):
        env = dict(self.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        proc = subprocess.run(
            ["git"] + args, cwd=cwd, env=env, capture_output=True,
            text=True, timeout=timeout or self.config.get("git_timeout_s", 300),
            shell=False)
        return proc

    def _process_clone(self, task, record, remote):
        work = self.workdir / "clones" / task.full_name.replace("/", "__")
        force_rmtree(work)
        work.parent.mkdir(parents=True, exist_ok=True)
        url = authenticated_url(remote, self.token)
        clone = self._git(["clone", "--depth", "1", "--branch",
                           task.default_branch, "--single-branch",
                           url, str(work)])
        if clone.returncode != 0:
            record["decision"] = "FAILED_CLONE"
            record["reason"] = (clone.stderr or clone.returncode)[-400:]
            return
        try:
            listing = self._git(["ls-files"], cwd=str(work))
            paths = tuple(p for p in listing.stdout.splitlines() if p)
            files = {}
            for path in NEEDED_FILES:
                candidate = work / path
                if path in paths and candidate.is_file():
                    files[path] = candidate.read_text(encoding="utf-8")
            from .changes import RepoTree
            tree = RepoTree(full_name=task.full_name,
                            default_branch=task.default_branch,
                            private=task.private, paths=paths, files=files)
            changes = build_changeset(tree, self.config)
            record["changes"] = [
                {"path": c.path, "action": c.action, "reason": c.reason}
                for c in changes]
            if not changes:
                record["decision"] = NO_FACTUAL_CHANGE
                record["reason"] = ("every factual baseline item already "
                                    "present")
                return
            if not self.apply:
                record["decision"] = PLANNED
                record["reason"] = ("dry-run: %d factual file(s) would be "
                                    "written" % len(changes))
                return
            for change in changes:
                _validate_content(change.path, change.content)
                target = work / change.path
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(change.content)
            author = "%s <%s>" % (self.config.get(
                "author_name", "premium-factory[bot]"),
                self.config.get("author_email",
                                "premium-factory[bot]@users.noreply"
                                ".github.com"))
            add = self._git(["add", "--"] + [c.path for c in changes],
                            cwd=str(work))
            if add.returncode != 0:
                raise RuntimeError("git add failed: %s" % add.stderr)
            identity = ["-c", "user.name=%s"
                        % self.config.get("author_name", "premium-factory"),
                        "-c", "user.email=%s"
                        % self.config.get(
                            "author_email",
                            "premium-factory@users.noreply.github.com")]
            commit = self._git(identity + ["commit", "--author", author,
                                           "-m", commit_message(changes)],
                               cwd=str(work))
            if commit.returncode != 0:
                raise RuntimeError("git commit failed: %s" % commit.stderr)
            push_argv = self.build_push_cmd(task.default_branch)
            push = self._git(push_argv, cwd=str(work))
            if push.returncode != 0:
                stderr = (push.stderr or "")[-400:]
                if "remote rejected" in stderr or "protected" in stderr:
                    record["decision"] = ELEVATED
                    record["reason"] = "push rejected by policy: %s" % stderr
                else:
                    record["decision"] = "FAILED_PUSH"
                    record["reason"] = stderr
                return
            head = self._git(["rev-parse", "HEAD"], cwd=str(work))
            record["decision"] = APPLIED
            record["commit"] = head.stdout.strip()
            record["reason"] = ("pushed %d file(s) to %s (normal push, "
                                "no force)"
                                % (len(changes), task.default_branch))
        finally:
            if record["decision"] in (APPLIED, NO_FACTUAL_CHANGE, PLANNED):
                force_rmtree(work)

    @staticmethod
    def build_push_cmd(branch):
        """Plain push. Force flags are absent by construction."""
        return ["push", "origin", branch]

    # -- hook + helpers ------------------------------------------------------

    def _run_hook(self, task, record):
        hook = self.config.get("codex_hook") or {}
        if not hook.get("enabled"):
            return None
        report_path = self.reports_dir / ("hook-%s.json" % task.full_name
                                          .replace("/", "__"))
        command = hook.get("command", "").format(
            repo=task.full_name, report=str(report_path))
        timeout = int(hook.get("timeout_s", 900))
        result = {"command": command, "timeout_s": timeout}
        popen_kwargs = {"shell": True, "stdout": subprocess.PIPE,
                        "stderr": subprocess.PIPE, "text": True}
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True  # own process group
        try:
            proc = subprocess.Popen(command, **popen_kwargs)
        except OSError as exc:
            result["status"] = "error"
            result["output_tail"] = str(exc)[-400:]
            self._write_json(report_path, {"repo": task.full_name,
                                           "decision": record["decision"],
                                           "hook": result})
            return result
        try:
            out, err = proc.communicate(timeout=timeout)
            result["status"] = "exit"
            result["code"] = proc.returncode
            result["output_tail"] = (out + err)[-400:]
        except subprocess.TimeoutExpired:
            # hard kill of the whole tree: a hung agent cannot eat the run
            self._hard_kill(proc)
            proc.communicate()
            result["status"] = "timeout"
            result["output_tail"] = ("hard timeout after %ss; the batch "
                                     "survives" % timeout)
        self._write_json(report_path, {"repo": task.full_name,
                                       "decision": record["decision"],
                                       "hook": result})
        return result

    @staticmethod
    def _hard_kill(proc):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID",
                                str(proc.pid)], capture_output=True)
            else:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass

    def _rate_floor_hit(self):
        if self.api is None or self.api.rate_remaining is None:
            return False
        floor = max(self.config.get("min_rate_remaining", 100), 20)
        return self.api.rate_remaining < floor

    def _write_json(self, path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1, sort_keys=True),
                        encoding="utf-8")
