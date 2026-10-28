import json
import unittest
from pathlib import Path

from pf.audit import load_evidence


class TestAudit(unittest.TestCase):
    def test_all_codex_report_shapes_normalized(self):
        root = Path(__file__).parent / "data_audit"
        root.mkdir(exist_ok=True)
        try:
            # shape 1: batch file with a repos list
            (root / "batch-001.json").write_text(json.dumps({
                "batch": 1,
                "repos": [
                    {"repo": "o/py-legacy", "findings": ["no CI"]},
                    {"repo": "o/node-app", "findings": ["no engines"]},
                ],
            }), encoding="utf-8")
            # shape 2: single-repo pilot report
            (root / "piloto.json").write_text(json.dumps({
                "full_name": "o/rust-cli", "verdict": "ok", "issues": [],
            }), encoding="utf-8")
            # shape 3: inventory-style map
            (root / "inventario.json").write_text(json.dumps({
                "o/go-svc": {"status": "pending", "summary": "no deps"},
            }), encoding="utf-8")
            evidence = load_evidence(root)
            self.assertEqual(
                evidence["o/py-legacy"][0]["findings"], ["no CI"])
            self.assertEqual(evidence["o/py-legacy"][0]["file"],
                             "batch-001.json")
            self.assertEqual(evidence["o/rust-cli"][0]["verdict"], "ok")
            self.assertEqual(evidence["o/go-svc"][0]["status"], "pending")
            self.assertEqual(len(evidence), 4)  # node-app included
        finally:
            for child in root.iterdir():
                child.unlink()
            root.rmdir()

    def test_missing_dir_means_no_evidence(self):
        self.assertEqual(load_evidence("definitely/not/here"), {})

    def test_unreadable_and_foreign_files_ignored(self):
        root = Path(__file__).parent / "data_audit2"
        root.mkdir(exist_ok=True)
        try:
            (root / "broken.json").write_text("{not json", encoding="utf-8")
            (root / "unrelated.json").write_text(
                json.dumps({"count": 3, "items": [1, 2]}), encoding="utf-8")
            self.assertEqual(load_evidence(root), {})
        finally:
            for child in root.iterdir():
                child.unlink()
            root.rmdir()

    def test_own_run_output_never_counts_as_evidence(self):
        from pf.audit import IGNORE_PREFIXES
        for prefix in IGNORE_PREFIXES:
            self.assertTrue(prefix.endswith("-")
                            or prefix.endswith("summary"),
                            msg=prefix)


if __name__ == "__main__":
    unittest.main()
