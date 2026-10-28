"""Atomic checkpoint so a dead session never means starting over.

Only terminal-success decisions are recorded as done (APPLIED,
NO_FACTUAL_CHANGE): a dry-run never writes anything here, and ELEVATED /
FAILED entries are retried automatically on resume.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

SCHEMA_VERSION = 2
DONE_DECISIONS = {"APPLIED", "NO_FACTUAL_CHANGE"}


class CheckpointError(Exception):
    pass


class Checkpoint:
    def __init__(self, path):
        self.path = Path(path)
        self.entries = {}
        self.meta = {}

    @classmethod
    def load(cls, path):
        cp = cls(path)
        if not cp.path.exists():
            return cp
        try:
            data = json.loads(cp.path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CheckpointError(
                "checkpoint %s is corrupt: %s" % (cp.path, exc))
        if not isinstance(data, dict):
            raise CheckpointError(
                "checkpoint %s is not a JSON object" % cp.path)
        cp.entries = data.get("entries", {}) or {}
        cp.meta = data.get("meta", {}) or {}
        return cp

    def record(self, full_name, entry):
        payload = dict(entry)
        payload["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.entries[full_name] = payload
        self._flush()

    def is_done(self, full_name):
        entry = self.entries.get(full_name)
        return bool(entry) and entry.get("decision") in DONE_DECISIONS

    def applied_names(self):
        return [name for name, entry in self.entries.items()
                if entry.get("decision") in DONE_DECISIONS]

    def counts(self):
        counts = {}
        for entry in self.entries.values():
            decision = entry.get("decision", "?")
            counts[decision] = counts.get(decision, 0) + 1
        return counts

    def _flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": SCHEMA_VERSION,
            "meta": self.meta,
            "entries": self.entries,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, self.path)  # atomic on POSIX and Windows
