"""Every state of the automatic public release."""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("follow_release", Path(__file__).resolve().parents[1] / "scripts" / "follow_release.py")
follow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(follow)

ARGS = ()


def pr(checks="green", in_queue=False, ejected=False, queue_checks="pending"):
    return {"id": "ID", "number": 7, "branch": "sync/1.0.0", "in_queue": in_queue, "ejected": ejected, "checks": checks,
            "queue_branch": "gh-readonly-queue/main/pr-7-abc" if in_queue else None, "queue_checks": queue_checks if in_queue else None}


def state(**changes):
    base = {"version": "1.0.0", "released": False, "main_version": "0.9.0", "delivered": True, "sync_pr": None, "runs": {}}
    base.update(changes)
    return base


class Decide(unittest.TestCase):
    def action(self, **changes):
        actions = follow.decide(state(**changes))
        self.assertEqual(len(actions), 1)
        return actions[0]

    def test_nothing_to_follow(self):
        self.assertEqual(follow.decide({"version": None})[0][0], "done")

    def test_released_is_done(self):
        self.assertEqual(self.action(released=True, main_version="1.0.0")[0], "done")

    def test_main_past_the_private_release_is_left_alone(self):
        self.assertEqual(self.action(main_version="1.1.0")[0], "done")

    def test_an_undelivered_release_is_never_synced(self):
        action = self.action(delivered=False)
        self.assertEqual(action[0], "notify")
        self.assertIn("deliver 1.0.0", action[1])

    def test_a_delivered_release_starts_the_sync(self):
        self.assertEqual(self.action(), ("dispatch", "public-sync"))

    def test_a_running_sync_is_not_started_twice(self):
        self.assertEqual(self.action(runs={"public-sync": "active"})[0], "wait")

    def test_a_failed_step_notifies_and_is_never_restarted(self):
        self.assertEqual(self.action(runs={"public-sync": "failed"})[0], "notify")
        self.assertEqual(self.action(main_version="1.0.0", runs={"publish-release": "failed"})[0], "notify")

    def test_a_sync_that_succeeded_without_a_pull_request_notifies(self):
        self.assertEqual(self.action(runs={"public-sync": "success"})[0], "notify")

    def test_a_pull_request_with_no_checks_gets_them_started(self):
        self.assertEqual(self.action(sync_pr=pr("none")), ("start-ci", "sync/1.0.0"))

    def test_a_pull_request_being_checked_waits(self):
        self.assertEqual(self.action(sync_pr=pr("pending"))[0], "wait")

    def test_a_green_pull_request_is_queued_once(self):
        self.assertEqual(self.action(sync_pr=pr()), ("enqueue", "ID"))
        self.assertEqual(self.action(sync_pr=pr(in_queue=True))[0], "wait")

    def test_a_queued_pull_request_with_no_checks_gets_them_started_on_the_queue_branch(self):
        self.assertEqual(self.action(sync_pr=pr(in_queue=True, queue_checks="none")), ("start-ci", "gh-readonly-queue/main/pr-7-abc"))

    def test_a_red_or_ejected_pull_request_notifies_and_is_not_requeued(self):
        self.assertEqual(self.action(sync_pr=pr("failed"))[0], "notify")
        self.assertEqual(self.action(sync_pr=pr(ejected=True))[0], "notify")

    def test_a_merged_sync_publishes(self):
        self.assertEqual(self.action(main_version="1.0.0"), ("dispatch", "publish-release"))

    def test_a_running_publish_is_not_started_twice(self):
        self.assertEqual(self.action(main_version="1.0.0", runs={"publish-release": "active"})[0], "wait")


class Reading(unittest.TestCase):
    def read(self, removed):
        answer = {"data": {"repository": {"pullRequests": {"nodes": [{
            "id": "ID", "number": 7, "headRefName": "sync/1.0.0", "isInMergeQueue": False,
            "timelineItems": {"nodes": removed},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]},
        }]}}}}
        original = follow.gh
        follow.gh = lambda *args, **kw: json.dumps(answer)
        self.addCleanup(setattr, follow, "gh", original)
        return follow.open_pr("o/n", "sync/1.0.0")

    def test_only_a_removal_event_is_an_ejection(self):
        self.assertFalse(self.read([])["ejected"])
        self.assertTrue(self.read([{"__typename": "RemovedFromMergeQueueEvent"}])["ejected"])

    def queued(self, total_count):
        def answer(*args, **kw):
            if args[1].startswith("graphql"):
                return json.dumps({"data": {"repository": {"pullRequests": {"nodes": [{
                    "id": "ID", "number": 7, "headRefName": "sync/1.0.0", "isInMergeQueue": True,
                    "timelineItems": {"nodes": []},
                    "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]},
                }]}}}})
            if "matching-refs" in args[1]:
                return json.dumps([{"ref": "refs/heads/gh-readonly-queue/main/pr-7-abc", "object": {"sha": "SHA"}}])
            self.assertIn("/commits/SHA/check-runs", args[1])
            return json.dumps({"total_count": total_count})
        original = follow.gh
        follow.gh = answer
        self.addCleanup(setattr, follow, "gh", original)
        return follow.open_pr("o/n", "sync/1.0.0")

    def test_a_queue_commit_with_check_runs_is_not_started_again(self):
        # GitHub's GraphQL rollup is null here even with check runs attached.
        pull = self.queued(15)
        self.assertEqual(pull["queue_checks"], "started")
        self.assertEqual(follow.advance_pr(*ARGS, pull)[0][0], "wait")

    def test_a_queue_commit_with_no_check_runs_gets_them_started(self):
        pull = self.queued(0)
        self.assertEqual(follow.advance_pr(*ARGS, pull), [("start-ci", "gh-readonly-queue/main/pr-7-abc")])

    def test_no_rollup_means_no_checks(self):
        self.assertEqual(self.read([])["checks"], "none")

    def test_a_started_workflow_without_its_gate_yet_is_pending(self):
        self.assertEqual(follow.checks_of([{"name": "other", "status": "COMPLETED", "conclusion": "SUCCESS"}]), "pending")

    def test_a_green_gate_is_green(self):
        self.assertEqual(follow.checks_of([{"name": "ci-gate", "status": "COMPLETED", "conclusion": "SUCCESS"}]), "green")


if __name__ == "__main__":
    unittest.main()
