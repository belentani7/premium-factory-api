import unittest

from pf.checkpoint import Checkpoint
from pf.planner import CALLS_PER_REPO, RepoTask, plan_batches


def make_tasks(names, **kwargs):
    return [RepoTask(full_name=n, default_branch="main", **kwargs)
            for n in names]


class FakeClient:
    def __init__(self, remaining, floor=100):
        self.rate_remaining = remaining
        self.min_rate_remaining = floor


class TestPlanner(unittest.TestCase):
    def test_batches_of_eight(self):
        tasks = make_tasks("r%02d" % i for i in range(20))
        planned = plan_batches(tasks)
        self.assertEqual([len(b) for b in planned.batches], [8, 8, 4])

    def test_deterministic_order_and_boundaries(self):
        names = ["o/%s" % s for s in ("zeta", "alpha", "mid", "Beta")]
        tasks = make_tasks(names)
        first = plan_batches(tasks)
        second = plan_batches(make_tasks(reversed(names)))
        flat_a = [t.full_name for b in first.batches for t in b]
        flat_b = [t.full_name for b in second.batches for t in b]
        # materialized sorted lists (a broken generator here would give [])
        self.assertEqual(sorted(names), ["o/Beta", "o/alpha", "o/mid",
                                         "o/zeta"])
        self.assertEqual(flat_a, flat_b)
        self.assertEqual(flat_a, sorted(names))

    def test_limit_applies_after_filters(self):
        tasks = make_tasks(["o/r%02d" % i for i in range(30)])
        tasks[3].archived = True
        tasks[7].fork = True
        planned = plan_batches(tasks, limit=10)
        total = sum(len(b) for b in planned.batches)
        self.assertEqual(total, 10)
        self.assertTrue(planned.limited)

    def test_archived_forks_and_unpushable_skipped(self):
        tasks = make_tasks(["o/a", "o/b", "o/c", "o/d"])
        tasks[0].archived = True
        tasks[1].fork = True
        tasks[2].pushable = False
        planned = plan_batches(tasks)
        self.assertEqual([t.full_name for b in planned.batches
                          for t in b], ["o/d"])
        decisions = {s["full_name"]: s["decision"] for s in planned.skipped}
        self.assertEqual(decisions["o/a"], "SKIPPED_ARCHIVED")
        self.assertEqual(decisions["o/b"], "SKIPPED_FORK")
        self.assertEqual(decisions["o/c"], "SKIPPED_NO_PUSH")

    def test_resume_skips_terminal_success_only(self):
        tasks = make_tasks(["o/a", "o/b", "o/c", "o/d"])
        checkpoint = Checkpoint("unused.json")
        checkpoint.entries = {
            "o/a": {"decision": "APPLIED"},
            "o/b": {"decision": "NO_FACTUAL_CHANGE"},
            "o/c": {"decision": "ELEVATED"},  # retried on resume
        }
        planned = plan_batches(tasks, checkpoint=checkpoint)
        pending = [t.full_name for b in planned.batches for t in b]
        self.assertEqual(pending, ["o/c", "o/d"])
        skipped = {s["full_name"]: s["decision"] for s in planned.skipped}
        self.assertEqual(skipped["o/a"], "ALREADY_APPLIED")
        self.assertEqual(skipped["o/b"], "ALREADY_APPLIED")

    def test_rate_budget_hard_cap(self):
        tasks = make_tasks(["o/r%02d" % i for i in range(50)])
        client = FakeClient(remaining=40, floor=20)
        planned = plan_batches(tasks, client=client)
        feasible = (40 - 20) // CALLS_PER_REPO  # 2
        total = sum(len(b) for b in planned.batches)
        self.assertEqual(total, feasible)
        self.assertTrue(planned.limited)

    def test_no_rate_data_means_no_cap(self):
        tasks = make_tasks(["o/r%02d" % i for i in range(20)])
        planned = plan_batches(tasks, client=FakeClient(None))
        self.assertFalse(planned.limited)
        self.assertEqual(sum(len(b) for b in planned.batches), 20)


if __name__ == "__main__":
    unittest.main()
