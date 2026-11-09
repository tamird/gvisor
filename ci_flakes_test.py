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
                         "exitCode": 2, "pr": 17269}])
        self.assertEqual(snapshot["pipelines"]["pipeline"]["pending"], [])

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
        with patch.object(ci, "latest_build", side_effect=[44, 48]), patch.object(ci, "inspect", return_value=([], True)) as read:
            state, _ = ci.scan("pipeline", state, NOW)
            self.assertEqual([c.args[1] for c in read.call_args_list], [32, 33, 41, 42, 43, 44])
            read.reset_mock()
            final, _ = ci.scan("pipeline", state, NOW)
        self.assertEqual([c.args[1] for c in read.call_args_list], [34, 32, 45, 46, 47, 48])
        self.assertEqual(final["nextBuild"], 49)
        self.assertCountEqual(final["pending"], [32, 33, 34, *range(41, 49)])

    def test_late_manual_retry(self):
        base = dict(BUILD, state="failed", finished_at=NOW)
        records = {"records": [job(FIRST, False)], "has_next_page": False}
        with patch.object(ci, "read", side_effect=[json.dumps(base), json.dumps(records)] * 2):
            self.assertEqual(ci.inspect("pipeline", 42, datetime.fromisoformat(NOW)), ([], True))
            self.assertEqual(ci.inspect("pipeline", 42, datetime.fromisoformat("2026-11-11T12:00:00+00:00")), ([], False))


if __name__ == "__main__":
    unittest.main()
