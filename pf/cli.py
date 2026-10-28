"""CLI: inventory | plan | run | status | notify.

Global flags (--token --api-url --workdir --config) are accepted BOTH
before and after the subcommand: the common parser is parented into every
subparser with argparse.SUPPRESS defaults, so a missing flag never
clobbers the value given earlier.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import __version__, ghapi
from .audit import attach_evidence, load_evidence
from .checkpoint import Checkpoint
from .executor import Executor
from .notify import notify_from_env
from .planner import from_inventory, plan_batches

DEFAULT_CONFIG = {
    "batch_size": 8,
    "sleep_s": 0.5,
    "min_rate_remaining": 100,
    "max_rate_wait": 900,
    "skip_forks": True,
    "node_min": ">=18",
    "node_ci_version": "18",
    "enable_codeql_private": False,
    "author_name": "premium-factory[bot]",
    "author_email": "premium-factory[bot]@users.noreply.github.com",
    "git_timeout_s": 300,
    "codex_hook": {
        "enabled": False,
        "timeout_s": 900,
        "command": "codex exec --full-auto "
                   "\"Review the factual quality files added to {repo} and "
                   "write findings to {report}\"",
    },
}

COMMANDS = ("inventory", "plan", "run", "status", "notify")


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--token", default=argparse.SUPPRESS,
                        help="GitHub PAT (default: GITHUB_TOKEN env)")
    common.add_argument("--api-url", default=argparse.SUPPRESS,
                        help="API base (default: GITHUB_API_URL env)")
    common.add_argument("--workdir", default=argparse.SUPPRESS,
                        help="state/reports directory (default: .)")
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="JSON config path")

    parser = argparse.ArgumentParser(
        prog="pf", parents=[common],
        description="premium-factory: factual quality baseline for GitHub "
                    "repos (0 markdown, force impossible, resumable)")
    parser.add_argument("--version", action="version",
                        version="premium-factory %s" % __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("inventory", parents=[common],
                         help="list every repo the token can see")
    inv.add_argument("--out", default="state/inventory.json")

    plan = sub.add_parser("plan", parents=[common],
                          help="show batches; touches nothing")
    plan.add_argument("--limit", type=int)
    plan.add_argument("--batch-size", type=int)
    plan.add_argument("--inventory", default="state/inventory.json")
    plan.add_argument("--only")
    plan.add_argument("--exclude")

    run = sub.add_parser("run", parents=[common],
                         help="dry-run by default; --apply commits to main")
    run.add_argument("--apply", action="store_true")
    run.add_argument("--mode", choices=("api", "clone"), default="api")
    run.add_argument("--limit", type=int)
    run.add_argument("--batch-size", type=int)
    run.add_argument("--sleep", type=float)
    run.add_argument("--audit-dir", default="reports")
    run.add_argument("--inventory", default="state/inventory.json")
    run.add_argument("--only")
    run.add_argument("--exclude")
    run.add_argument("--no-resume", dest="resume", action="store_false")

    sub.add_parser("status", parents=[common],
                   help="checkpoint + last run summary")
    sub.add_parser("notify", parents=[common],
                   help="email the last run summary (PF_SMTP_* env)")
    return parser


def load_config(path):
    config = dict(DEFAULT_CONFIG)
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for key, value in data.items():
            if key == "codex_hook" and isinstance(value, dict):
                config["codex_hook"] = dict(DEFAULT_CONFIG["codex_hook"],
                                            **value)
            else:
                config[key] = value
    return config


def _resolve(args):
    workdir = Path(getattr(args, "workdir", None) or ".")
    config_path = getattr(args, "config", None)
    config = load_config(config_path)
    token = getattr(args, "token", None) or os.environ.get("GITHUB_TOKEN") \
        or config.get("token")
    api_url = getattr(args, "api_url", None) or os.environ.get(
        "GITHUB_API_URL") or config.get("api_url") or "https://api.github.com"
    return workdir, config, token, api_url


def _make_api(args, config, token, api_url):
    return ghapi.GitHubAPI(
        token, api_url,
        min_rate_remaining=config["min_rate_remaining"],
        max_rate_wait=config["max_rate_wait"])


def _filter(tasks, only, exclude):
    result = tasks
    if only:
        rx = re.compile(only)
        result = [t for t in result if rx.search(t.full_name)]
    if exclude:
        rx = re.compile(exclude)
        result = [t for t in result if not rx.search(t.full_name)]
    return result


def _load_inventory_or_live(args, api, inventory_path):
    path = Path(inventory_path)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return build_inventory(api)


def build_inventory(api):
    user = api.get_user()
    repos = api.list_repos()
    return {
        "tool": "premium-factory",
        "login": user.get("login"),
        "generated_at": __import__("time").strftime(
            "%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
        "total": len(repos),
        "pushable": sum(1 for r in repos
                        if (r.get("permissions") or {}).get("push")),
        "repos": repos,
    }


def cmd_inventory(args):
    workdir, config, token, api_url = _resolve(args)
    if not token:
        print("error: no token. Set GITHUB_TOKEN or pass --token "
              "(fine-grained: Metadata R, Contents RW, Workflows RW)")
        return 2
    api = _make_api(args, config, token, api_url)
    inventory = build_inventory(api)
    out = Path(getattr(args, "out", None) or workdir / "state/inventory.json")
    if not Path(out).is_absolute():
        out = workdir / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(inventory, indent=1, sort_keys=True),
                   encoding="utf-8")
    print("inventory: login=%s total=%d pushable=%d -> %s"
          % (inventory["login"], inventory["total"], inventory["pushable"],
             out))
    return 0


def cmd_plan(args):
    workdir, config, token, api_url = _resolve(args)
    inventory_path = Path(args.inventory)
    if not inventory_path.is_absolute():
        inventory_path = workdir / inventory_path
    if inventory_path.exists():
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        api = None
    else:
        if not token:
            print("error: no inventory at %s and no token to build one"
                  % inventory_path)
            return 2
        api = _make_api(args, config, token, api_url)
        inventory = build_inventory(api)
    tasks = _filter(from_inventory(inventory), args.only, args.exclude)
    planned = plan_batches(tasks, batch_size=args.batch_size
                           or config["batch_size"], limit=args.limit,
                           skip_forks=config["skip_forks"], client=api)
    for index, batch in enumerate(planned.batches, 1):
        print("batch %03d (%d): %s" % (
            index, len(batch), " ".join(t.full_name for t in batch)))
    skipped = planned.skipped
    if skipped:
        by_decision = {}
        for item in skipped:
            by_decision.setdefault(item["decision"], []).append(
                item["full_name"])
        for decision, names in sorted(by_decision.items()):
            print("%s (%d): %s" % (decision, len(names),
                                   " ".join(sorted(names))))
    print("plan: %d repo(s) in %d batch(es), %d skipped%s" % (
        sum(len(b) for b in planned.batches), len(planned.batches),
        len(skipped), " [LIMITED by --limit/rate-budget]"
        if planned.limited else ""))
    return 0


def cmd_run(args):
    workdir, config, token, api_url = _resolve(args)
    if not token:
        print("error: no token. Set GITHUB_TOKEN or pass --token")
        return 2
    api = _make_api(args, config, token, api_url)
    inventory_path = Path(args.inventory)
    if not inventory_path.is_absolute():
        inventory_path = workdir / inventory_path
    inventory = _load_inventory_or_live(args, api, inventory_path)
    if not inventory_path.exists():
        inventory_path.parent.mkdir(parents=True, exist_ok=True)
        inventory_path.write_text(json.dumps(inventory, indent=1,
                                             sort_keys=True),
                                  encoding="utf-8")

    tasks = _filter(from_inventory(inventory), args.only, args.exclude)
    audit_dir = Path(args.audit_dir)
    if not audit_dir.is_absolute():
        audit_dir = workdir / audit_dir
    attach_evidence(tasks, load_evidence(audit_dir))

    checkpoint_path = workdir / "state" / "checkpoint.json"
    checkpoint = Checkpoint.load(checkpoint_path) if args.resume else None

    planned = plan_batches(
        tasks, batch_size=args.batch_size or config["batch_size"],
        limit=args.limit, checkpoint=checkpoint,
        skip_forks=config["skip_forks"], client=api)

    if not args.apply:
        print("DRY-RUN: nothing will be written (use --apply to commit)")
    executor = Executor(
        api, workdir, mode=args.mode, apply=args.apply, config=config,
        audit_dir=args.audit_dir, checkpoint=checkpoint
        if args.apply else None, sleep_s=args.sleep
        if args.sleep is not None else config["sleep_s"], token=token)
    try:
        summary = executor.run(planned.batches, planned.skipped)
    except ghapi.RateLimitExceeded as exc:
        print("stopped cleanly at rate floor: %s" % exc)
        print("re-run the same command to resume; nothing is lost")
        return 1
    counts = summary["counts"]
    print("run: %s mode=%s apply=%s processed=%d incomplete=%s" % (
        "done" if not summary["incomplete"] else "stopped",
        args.mode, args.apply, summary["processed"],
        summary["incomplete"]))
    for decision in sorted(counts):
        print("  %-18s %d" % (decision, counts[decision]))
    if args.apply and summary["pending_rate"]:
        print("pending (rate): %s" % ", ".join(summary["pending_rate"]))
    if args.apply:
        sent, reason = notify_from_env(summary)
        print("notify: %s" % reason if not sent else "notify: sent (%s)"
              % reason)
    bad = sum(v for k, v in counts.items()
              if k.startswith("FAILED") or k == "ELEVATED")
    return 0 if not bad else 1


def cmd_status(args):
    workdir, config, token, api_url = _resolve(args)
    checkpoint = Checkpoint.load(workdir / "state" / "checkpoint.json")
    summary_path = workdir / "reports" / "run-summary.json"
    print("checkpoint: %d entr(ies)" % len(checkpoint.entries))
    counts = checkpoint.counts()
    for decision in sorted(counts):
        print("  %-18s %d" % (decision, counts[decision]))
    retriable = [name for name, entry in checkpoint.entries.items()
                 if entry.get("decision") not in ("APPLIED",
                                                  "NO_FACTUAL_CHANGE")]
    if retriable:
        print("retry-on-resume (%d): %s" % (len(retriable),
                                            " ".join(sorted(retriable))))
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        print("last run: %s mode=%s apply=%s processed=%d" % (
            summary.get("finished_at"), summary.get("mode"),
            summary.get("apply"), summary.get("processed")))
    else:
        print("no run summary yet")
    return 0


def cmd_notify(args):
    workdir, config, token, api_url = _resolve(args)
    summary_path = workdir / "reports" / "run-summary.json"
    if not summary_path.exists():
        print("no run summary to notify about")
        return 1
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    sent, reason = notify_from_env(summary)
    print("notify: %s" % reason)
    return 0 if sent else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "inventory": cmd_inventory, "plan": cmd_plan, "run": cmd_run,
        "status": cmd_status, "notify": cmd_notify,
    }
    return handlers[args.command](args)
