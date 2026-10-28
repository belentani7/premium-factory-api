import unittest
from pathlib import Path

from pf.checkpoint import Checkpoint, CheckpointError


class TestCheckpoint(unittest.TestCase):
    def test_record_is_atomic_and_reloadable(self):
        path = self.tmp / "state" / "checkpoint.json"
        cp = Checkpoint(path)
        cp.record("o/r1", {"decision": "APPLIED", "commit": "abc"})
        cp.record("o/r2", {"decision": "ELEVATED"})
        self.assertFalse(path.with_suffix(".tmp").exists())
        reloaded = Checkpoint.load(path)
        self.assertEqual(reloaded.entries["o/r1"]["commit"], "abc")
        self.assertEqual(reloaded.entries["o/r2"]["decision"], "ELEVATED")

    def test_only_terminal_success_is_done(self):
        cp = Checkpoint(self.tmp / "cp.json")
        cp.entries = {
            "o/applied": {"decision": "APPLIED"},
            "o/clean": {"decision": "NO_FACTUAL_CHANGE"},
            "o/elev": {"decision": "ELEVATED"},
            "o/failed": {"decision": "FAILED_PUSH"},
        }
        self.assertTrue(cp.is_done("o/applied"))
        self.assertTrue(cp.is_done("o/clean"))
        self.assertFalse(cp.is_done("o/elev"))
        self.assertFalse(cp.is_done("o/failed"))
        self.assertEqual(sorted(cp.applied_names()), ["o/applied",
                                                      "o/clean"])
        self.assertEqual(cp.counts(), {"APPLIED": 1, "ELEVATED": 1,
                                       "FAILED_PUSH": 1,
                                       "NO_FACTUAL_CHANGE": 1})

    def test_corrupt_checkpoint_raises_cleanly(self):
        path = self.tmp / "cp.json"
        path.write_text("{ this is not json", encoding="utf-8")
        with self.assertRaises(CheckpointError) as ctx:
            Checkpoint.load(path)
        self.assertIn(str(path), str(ctx.exception))

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.__enter__())

    def tearDown(self):
        self._tmpdir.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
