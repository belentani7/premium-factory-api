"""Read ALL prior Codex audit reports and attach them as per-repo evidence.

Tolerant normalization: every ``*.json`` under ``audit_dir`` (recursive) is
scanned; any dict carrying a repo identity under ``repo`` / ``full_name`` /
``repository`` becomes one evidence item for that repo, citing the file.
No repo evidence is ever invented: absent mention means an empty list.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_KEYS = ("repo", "full_name", "repository")
EVIDENCE_KEYS = ("findings", "issues", "problems", "verdict", "status",
                 "summary", "audited", "result")
# files the executor itself writes into reports/ are not Codex audits
IGNORE_PREFIXES = ("api-batch-", "clone-batch-", "run-summary", "hook-")
# inventory-style reports map "owner/name" -> {...}
REPO_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _repo_of(node):
    if not isinstance(node, dict):
        return None
    for key in REPO_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _evidence_item(rel_name, node):
    item = {"file": rel_name}
    for key in EVIDENCE_KEYS:
        if key in node:
            item[key] = node[key]
    return item


def _walk_json(node, rel_name, out):
    if isinstance(node, list):
        for child in node:
            _walk_json(child, rel_name, out)
        return
    if not isinstance(node, dict):
        return
    repo = _repo_of(node)
    if repo is not None:
        out.setdefault(repo, []).append(_evidence_item(rel_name, node))
    for key, child in node.items():
        if isinstance(child, dict):
            if (_repo_of(child) is None and isinstance(key, str)
                    and REPO_KEY_PATTERN.match(key)):
                # {"o/repo": {"status": ...}} — repo named by the key
                out.setdefault(key, []).append(_evidence_item(rel_name,
                                                              child))
        if isinstance(child, (dict, list)):
            _walk_json(child, rel_name, out)


def load_evidence(audit_dir):
    """Map full_name -> [evidence items] from every JSON in audit_dir."""
    root = Path(audit_dir)
    if not root.is_dir():
        return {}
    evidence = {}
    for path in sorted(root.rglob("*.json")):
        if path.name.startswith(IGNORE_PREFIXES):
            continue  # our own run output, never audit evidence
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # unreadable report: skipped, never invented
        rel = path.name
        found = {}
        _walk_json(data, rel, found)
        for repo, items in found.items():
            evidence.setdefault(repo, []).extend(items)
    return evidence


def attach_evidence(tasks, evidence_map):
    for task in tasks:
        task.evidence = list(evidence_map.get(task.full_name, []))
    return tasks
