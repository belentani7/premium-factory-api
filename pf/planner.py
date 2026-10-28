"""Deterministic batching with hard limits.

Batches of ``batch_size`` (default 8, per the factory plan). Hard limits:
``--limit`` caps repos processed this run; a rate-budget floor caps what is
plannable at all; archived/fork/unpushable repos are recorded, never touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CALLS_PER_REPO = 8  # ref + tree + contents + blobs/trees/commit/ref (+1 margin)


@dataclass
class RepoTask:
    full_name: str
    default_branch: str
    private: bool = False
    archived: bool = False
    fork: bool = False
    language: str = ""
    clone_url: str = ""
    pushable: bool = True
    evidence: list = field(default_factory=list)

    def to_meta(self):
        return {
            "full_name": self.full_name,
            "default_branch": self.default_branch,
            "private": self.private,
            "archived": self.archived,
            "fork": self.fork,
            "language": self.language,
            "clone_url": self.clone_url,
            "pushable": self.pushable,
        }


def from_inventory(inventory):
    """Build tasks from an inventory dict (state/inventory.json shape)."""
    tasks = []
    for repo in inventory.get("repos", []):
        perms = repo.get("permissions") or {}
        tasks.append(RepoTask(
            full_name=repo["full_name"],
            default_branch=repo.get("default_branch") or "main",
            private=bool(repo.get("private")),
            archived=bool(repo.get("archived")),
            fork=bool(repo.get("fork")),
            language=repo.get("language") or "",
            clone_url=repo.get("clone_url") or "",
            pushable=bool(perms.get("push", True)),
        ))
    return tasks


@dataclass
class PlannedRun:
    batches: list
    skipped: list  # [{full_name, decision, reason}]
    limited: bool = False


def plan_batches(tasks, *, batch_size=8, limit=None, checkpoint=None,
                 skip_forks=True, client=None):
    """Deterministic plan. limit counts repos actually to be processed."""
    size = max(1, int(batch_size))
    skipped = []
    pending = []
    applied_names = set()
    if checkpoint is not None:
        applied_names = set(checkpoint.applied_names())

    for task in sorted(tasks, key=lambda t: t.full_name):
        if checkpoint is not None and task.full_name in applied_names:
            skipped.append({"full_name": task.full_name,
                            "decision": "ALREADY_APPLIED",
                            "reason": "checkpoint"})
            continue
        if task.archived:
            skipped.append({"full_name": task.full_name,
                            "decision": "SKIPPED_ARCHIVED",
                            "reason": "archived repos are never modified"})
            continue
        if task.fork and skip_forks:
            skipped.append({"full_name": task.full_name,
                            "decision": "SKIPPED_FORK",
                            "reason": "forks are never modified by default"})
            continue
        if not task.pushable:
            skipped.append({"full_name": task.full_name,
                            "decision": "SKIPPED_NO_PUSH",
                            "reason": "token lacks push permission"})
            continue
        pending.append(task)

    feasible = None
    if client is not None and client.rate_remaining is not None:
        headroom = client.rate_remaining - client.min_rate_remaining
        feasible = max(0, headroom // CALLS_PER_REPO)

    cap = limit if limit is not None else feasible
    if feasible is not None:
        cap = feasible if limit is None else min(limit, feasible)
    limited = cap is not None and len(pending) > cap
    if cap is not None:
        pending = pending[:cap]

    batches = [pending[i:i + size] for i in range(0, len(pending), size)]
    return PlannedRun(batches=batches, skipped=skipped, limited=limited)
