from datetime import datetime
import json
import unittest
from unittest.mock import patch

import ci_flakes as ci


NOW = "2026-11-09T12:30:00+00:00"
HEAD = "c" * 40
FIRST, SECOND, LAST, OTHER = [f"00000000-0000-0000-0000-{i:012x}" for i in range(4)]
BUILD = {"number": 42, "path": "/gvisor/pipeline/builds/42", "state": "passed", "commit_id": HEAD,
         "pull_request": {"url": "https://github.com/google/gvisor/pull/17269"}}


def job(key, passed, retry=None, shard=1):
    return {"id": key, "name": "unit", "type": "script", "state": "finished", "passed": passed,
            "exit_status": 0 if passed else 2, "parallel_group_index": shard, "finished_at": NOW,
            "retried_in_job_uuid": retry}


class FlakeTest(unittest.TestCase):
    def test_green_build_with_multiple_retry_attempts_and_pages(self):
        url = f"{ci.ROOT}/pipeline/builds"
        pages = {
            url: b'<a href="/gvisor/pipeline/builds/42">42</a>',
            url + "/42.json": json.dumps(BUILD).encode(),
            url + "/42/data/jobs": json.dumps({"records": [job(FIRST, False, SECOND),
                                                            job(OTHER, True, shard=2)],
                                               "has_next_page": True, "end_cursor": "page/2+"}).encode(),
            url + "/42/data/jobs?after=page%2F2%2B": json.dumps({
                "records": [job(SECOND, False, LAST), job(LAST, True)], "has_next_page": False}).encode(),
        }
        previous = {"pipelines": {"pipeline": {"nextBuild": 42, "firstBuild": 42}}}
        with patch.object(ci, "PIPELINES", ("pipeline",)), patch.object(ci, "read", side_effect=pages.__getitem__):
            snapshot = ci.collect(previous, NOW)
            replay = ci.collect(snapshot, NOW)
        self.assertEqual(snapshot, replay)
        self.assertEqual(snapshot["observations"], [{"pipeline": "pipeline", "build": 42, "head": HEAD,
                         "job": "unit", "failedJob": FIRST, "passedJob": LAST, "finishedAt": NOW,
                         "exitCode": 2, "failedState": "finished", "pr": 17269}])
        self.assertEqual(snapshot["pipelines"]["pipeline"]["pending"], [])

    def test_first_attempt_times_out_before_a_successful_retry(self):
        first = dict(job(FIRST, False, LAST), state="timed_out", exit_status=-1)
        result, = ci.recoveries("release", 42, BUILD, {FIRST: first, LAST: job(LAST, True)})
        self.assertEqual((result["failedState"], result["exitCode"]), ("timed_out", -1))

    def test_missing_cyclic_or_wrong_shard_successor_is_incomplete(self):
        start = job(FIRST, False, SECOND)
        for second in (None, job(SECOND, False, FIRST), job(SECOND, True, shard=2),
                       dict(job(SECOND, True), group_uuid=OTHER), dict(job(SECOND, True), retry_pending=True)):
            with self.subTest(second=second), self.assertRaises(ValueError):
                jobs = {FIRST: start}
                if second:
                    jobs[SECOND] = second
                ci.recoveries("pipeline", 42, BUILD, jobs)

    def test_retry_must_pass_and_cancellations_are_not_failures(self):
        for update in ({"state": "canceled", "passed": False},
                       {"state": "finished", "passed": False}):
            start, retry = job(FIRST, False, LAST), job(LAST, True)
            retry.update(update)
            self.assertEqual(ci.recoveries("pipeline", 42, BUILD, {FIRST: start, LAST: retry}), [])
        canceled = dict(job(FIRST, False, LAST), state="canceled")
        self.assertEqual(ci.recoveries("pipeline", 42, BUILD, {FIRST: canceled, LAST: job(LAST, True)}), [])
        with self.assertRaisesRegex(ValueError, "Retry still active"):
            ci.recoveries("pipeline", 42, BUILD, {FIRST: job(FIRST, False, LAST),
                                                  LAST: dict(job(LAST, True), state="running")})

    def test_provider_error_retains_evidence(self):
        observation = {"pipeline": "pipeline", "failedJob": FIRST, "finishedAt": NOW}
        previous = {"pipelines": {"pipeline": {"nextBuild": 43, "pending": [41]}},
                    "observations": [observation]}
        with patch.object(ci, "PIPELINES", ("pipeline",)), patch.object(ci, "read", side_effect=TimeoutError("unavailable")):
            result = ci.collect(previous, NOW)
        self.assertEqual(result["observations"], [observation])
        self.assertEqual(result["pipelines"]["pipeline"], {"checkedAt": NOW, "nextBuild": 43, "pending": [41],
                         "status": "unavailable", "error": "unavailable"})

    def test_jobs_revisited_independently_of_new_builds(self):
        state = {"firstBuild": 30, "latest": 40, "nextBuild": 41, "pending": [32, 33, 34]}
        with patch.object(ci, "listed_builds", side_effect=[(44, []), (48, [])]), patch.object(ci, "inspect", return_value=([], True, "running")) as read:
            state, _ = ci.scan("pipeline", state, NOW)
            self.assertEqual([c.args[1] for c in read.call_args_list], [32, 33, 41, 42, 43, 44])
            read.reset_mock()
            final, _ = ci.scan("pipeline", state, NOW)
        self.assertEqual([c.args[1] for c in read.call_args_list], [34, 32, 45, 46, 47, 48])
        self.assertEqual(final["nextBuild"], 49)
        self.assertCountEqual(final["pending"], [32, 33, 34, *range(41, 49)])

    def test_finished_builds_ahead_of_cursor_surface_before_catchup(self):
        rows = "".join(
            f'<turbo-frame data-reload-src="/gvisor/pipeline/builds/{n}/row">'
            f'<turbo-react-component props="{{&quot;state&quot;:&quot;{state}&quot;}}"></turbo-react-component></turbo-frame>'
            for n, state in [(105, "RUNNING"), (104, "PASSED"), (103, "FAILED"), (102, "PASSED")]
        )

        def inspect(_slug, n, _now):
            return ([{"build": n}] if n == 104 else [], n == 103, "failed" if n == 103 else "passed")

        with patch.object(ci, "read", return_value=rows.encode()), patch.object(ci, "inspect", side_effect=inspect) as read:
            state, found = ci.scan("pipeline", {"nextBuild": 41, "pending": [35, 36, 37]}, NOW)
            self.assertEqual(found, [{"build": 104}])
            self.assertEqual({c.args[1] for c in read.call_args_list}, {104, 103, 35, 36, 41, 42})
            self.assertEqual((state["nextBuild"], state["recentBuilds"], state["pending"]), (43, [103, 104], [37, 103]))
            read.reset_mock()
            state, _ = ci.scan("pipeline", state, NOW)
            self.assertEqual({c.args[1] for c in read.call_args_list}, {105, 102, 37, 103, 43, 44})
            self.assertEqual((state["nextBuild"], state["recentBuilds"], state["pending"]), (45, [102, 103, 104, 105], [103]))

            # Catch-up accounts for the sampled builds without fetching them twice.
            read.reset_mock()
            state, _ = ci.scan("pipeline", dict(state, nextBuild=101), NOW)
            self.assertEqual([c.args[1] for c in read.call_args_list], [103, 101])
            self.assertEqual((state["nextBuild"], state["recentBuilds"], state["pending"]), (106, [], [103]))

    def test_recent_running_build_is_revisited_as_soon_as_it_finishes(self):
        state = {"nextBuild": 41, "recentBuilds": [95], "pending": [1, 2, 3, 4, 95]}

        def inspect(_slug, n, _now):
            return ([{"build": n}] if n == 95 else [], n == 95, "failed" if n == 95 else "passed")

        with patch.object(ci, "listed_builds", return_value=(100, [(95, "failed")])), \
             patch.object(ci, "inspect", side_effect=inspect) as read:
            state, found = ci.scan("pipeline", state, NOW)
            self.assertEqual(found, [{"build": 95}])
            self.assertEqual((read.call_args_list[0].args[1], state["recentStates"]["95"]), (95, "failed"))
            read.reset_mock()
            state, _ = ci.scan("pipeline", state, NOW)
            self.assertNotIn(95, [c.args[1] for c in read.call_args_list])
        with patch.object(ci, "listed_builds", return_value=(100, [(95, "passed")])), \
             patch.object(ci, "inspect", return_value=([], False, "passed")) as read:
            state, _ = ci.scan("pipeline", state, NOW)
            self.assertEqual(read.call_args_list[0].args[1], 95)
            self.assertNotIn(95, state["pending"])

    def test_time_budget_preserves_unvisited_history_and_pending_retry(self):
        with patch.object(ci, "listed_builds", return_value=(101, [(100, "passed"), (99, "passed")])), \
             patch.object(ci, "inspect", return_value=([], False, "passed")) as inspect, \
             patch.object(ci.time, "monotonic", side_effect=[0, 1, 26]):
            state, _ = ci.scan("pipeline", {"nextBuild": 41, "pending": [25, 26]}, NOW)
        self.assertEqual([c.args[1] for c in inspect.call_args_list], [100])
        self.assertEqual((state["status"], state["nextBuild"], state["pending"], state["recentBuilds"]),
                         ("partial", 41, [25, 26], [100]))

    def test_full_retry_queue_keeps_old_obligations_and_cursor(self):
        with patch.object(ci, "listed_builds", return_value=(100, [(99, "passed"), (98, "passed")])), \
             patch.object(ci, "MAX_PENDING", 2), patch.object(ci, "inspect", return_value=([], True, "running")):
            state, _ = ci.scan("pipeline", {"nextBuild": 41, "pending": [25, 26]}, NOW)
        self.assertEqual((state["status"], state["nextBuild"], state["pending"], state["recentBuilds"]),
                         ("partial", 41, [25, 26], []))

    def test_late_manual_retry(self):
        base = dict(BUILD, state="failed", finished_at=NOW)
        records = {"records": [job(FIRST, False)], "has_next_page": False}
        with patch.object(ci, "read", side_effect=[json.dumps(base), json.dumps(records)] * 2):
            self.assertEqual(ci.inspect("pipeline", 42, datetime.fromisoformat(NOW)), ([], True, "failed"))
            self.assertEqual(ci.inspect("pipeline", 42, datetime.fromisoformat("2026-11-11T12:00:00+00:00")), ([], False, "failed"))


if __name__ == "__main__":
    unittest.main()
