"""GitHub Actions retry observations from the public run and job APIs."""

import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import ci_actions


HEAD = "a" * 40
NOW = "2026-11-09T12:00:00+00:00"
RUN = {"id": 42, "run_attempt": 2, "event": "pull_request", "path": ".github/workflows/lint.yml",
       "status": "completed", "conclusion": "success", "head_sha": HEAD, "pull_requests": [],
       "actor": {"login": "other-contributor"}}
PAGE = {"workflow_runs": [RUN]}


def job(attempt: int, result: str) -> dict:
    return {"id": 110 + attempt, "run_id": RUN["id"], "run_attempt": attempt, "head_sha": HEAD,
            "name": "lint", "status": "completed", "conclusion": result,
            "started_at": f"2026-11-09T0{attempt}:00:00Z", "completed_at": f"2026-11-09T0{attempt}:05:00Z",
            "steps": [{"name": "Run make lint", "conclusion": result}]}


class GitHubActionsRetries(unittest.TestCase):
    def test_one_spare_request_can_make_progress_on_both_reads(self):
        failed, passed = job(1, "failure"), job(2, "success")
        jobs = {"total_count": 2, "jobs": [passed, failed]}
        with mock.patch.object(ci_actions, "_read", side_effect=[PAGE, jobs]) as read:
            empty = ci_actions.collect({"cursor": 1}, NOW, budget=0)
            self.assertEqual(empty["requestsUsed"], 0)
            first = ci_actions.collect(empty, NOW, budget=1)
            self.assertEqual((first["requestsUsed"], first["cursor"], len(first["pending"])), (1, 2, 1))
            result = ci_actions.collect(first, NOW, budget=1)
            self.assertEqual(result["requestsUsed"], 1)
            self.assertEqual(result["observations"], [{
                "run": 42, "workflow": "Lint", "job": "lint", "head": HEAD, "failedAttempt": 1,
                "passedAttempt": 2, "failedJob": 111, "passedJob": 112, "finishedAt": passed["completed_at"],
            }])
            self.assertEqual(ci_actions.collect(result, NOW, budget=2)["requestsUsed"], 0)
            self.assertEqual(read.call_args_list, [
                mock.call(ci_actions.ACTIONS + "/workflows/lint.yml/runs?event=pull_request&per_page=100"),
                mock.call(ci_actions.ACTIONS + "/runs/42/jobs?filter=all&per_page=100"),
            ])

    def test_failed_attempt_comparisons_require_the_same_validation(self):
        failed, passed = job(1, "failure"), job(2, "success")
        for jobs in (
            [failed, {**passed, "head_sha": "b" * 40}], [failed, {**passed, "run_id": 43}],
            [failed, {**passed, "name": "lint (arm64)"}], [failed, {**failed, "id": 99}, passed],
            [failed, {**passed, "run_attempt": 3}], [failed, {**passed, "id": failed["id"]}],
            [failed, job(2, "failure")], [job(1, "cancelled"), passed],
            [{**failed, "steps": [{"name": "Set up job", "conclusion": "failure"}]}, passed],
            [failed, {**passed, "steps": [{"name": "Run make lint", "conclusion": "skipped"}]}],
        ):
            with self.subTest(jobs=jobs), mock.patch.object(
                    ci_actions, "_read", side_effect=[PAGE, {"total_count": len(jobs), "jobs": jobs}]):
                self.assertEqual(ci_actions.collect({"cursor": 1}, NOW, budget=8)["observations"], [])

    def test_outage_keeps_previous_observations_and_candidate(self):
        old = {"run": 9, "workflow": "Lint", "job": "lint", "head": "c" * 40, "failedJob": 31,
               "passedJob": 32, "finishedAt": "2026-11-08T10:00:00Z"}
        with mock.patch.object(ci_actions, "_read", side_effect=[PAGE, ValueError("GitHub Actions returned HTTP 503")]):
            stale = ci_actions.collect({"cursor": 1, "observations": [old]}, NOW, budget=2)
        self.assertEqual((stale["status"], stale["requestsUsed"], stale["observations"]), ("unavailable", 2, [old]))
        self.assertEqual((stale["cursor"], stale["pending"][0]["run"]), (2, 42))
        with mock.patch.object(ci_actions, "_read") as read:
            self.assertEqual(ci_actions.collect(stale, NOW, budget=2)["requestsUsed"], 0)
            read.assert_not_called()
        with mock.patch.object(ci_actions, "_read", return_value={"total_count": 0, "jobs": []}):
            retry = ci_actions.collect(stale, "2026-11-09T13:00:00Z", budget=1)
        self.assertEqual((retry["cursor"], retry["pending"], retry["observations"]), (2, stale["pending"], [old]))

    def test_incomplete_job_page_does_not_establish_uniqueness(self):
        jobs = [job(attempt, "failure") for attempt in range(1, 100)] + [job(100, "success")]
        with mock.patch.object(ci_actions, "_read", side_effect=[
                {"workflow_runs": [{**RUN, "run_attempt": 102}]}, {"total_count": 102, "jobs": jobs}]):
            state = ci_actions.collect({"cursor": 1}, NOW, budget=2)
        self.assertEqual((state["requestsUsed"], state["observations"], state["pending"]), (2, [], []))
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["partialRun"], {"workflow": "Lint", "run": 42, "head": HEAD, "attempt": 102,
                                               "observed": 100, "total": 102, "checkedAt": NOW})
        self.assertIn(f"42/102/{HEAD}", state["seen"])

    def test_search_reports_and_fixes_are_separate_from_retry_evidence(self):
        row = {"number": 1, "state": "open", "title": "A flaky test", "updated_at": NOW,
               "repository_url": "https://api.github.com/repos/google/gvisor",
               "html_url": "https://github.com/google/gvisor/issues/1"}
        rows = [{**row, "number": i, "html_url": f"https://github.com/google/gvisor/issues/{i}"}
                for i in range(1, 25)]
        rows.append({**row, "number": 25, "html_url": "https://github.com/google/gvisor/pull/25",
                     "state": "closed", "pull_request": {"merged_at": NOW}})
        with mock.patch.object(ci_actions, "_read", return_value={
                "total_count": 26, "incomplete_results": False, "items": rows}) as read:
            state = ci_actions.collect(None, NOW, budget=1)
        url = urlsplit(read.call_args.args[0])
        query = parse_qs(url.query)
        self.assertEqual((url.path, query["per_page"]), ("search/issues", ["25"]))
        self.assertEqual(query["q"], ["repo:google/gvisor in:title (flake OR flaky OR intermittent) "
                                      "updated:2026-08-11T12:00:00+00:00..2026-11-09T12:00:00+00:00 sort:updated-desc"])
        reports = state["reports"]
        self.assertEqual((reports["hasMore"], reports["total"], len(reports["items"])), (True, 26, 25))
        self.assertEqual([reports["items"][0]["kind"], reports["items"][-1]["state"]], ["issue", "merged"])
        self.assertEqual((state["observations"], state["status"], state["requestsUsed"]), ([], "deferred", 1))

        with mock.patch.object(ci_actions, "_read", return_value={
                "total_count": 1, "incomplete_results": True, "items": []}):
            retry = ci_actions.collect({"reports": reports}, NOW, budget=1)
        self.assertEqual((retry["cursor"], retry["reports"]["status"], retry["reports"]["items"]),
                         (1, "unavailable", reports["items"]))

    def test_broken_workflow_does_not_pin_discovery(self):
        with mock.patch.object(ci_actions, "_read", side_effect=[ValueError("HTTP 403"), {"workflow_runs": []}]) as read:
            state = ci_actions.collect({"cursor": 1}, NOW, budget=1)
            self.assertEqual((state["cursor"], state["requestsUsed"], state["status"]), (2, 1, "unavailable"))
            state = ci_actions.collect(state, "2026-11-09T13:00:00Z", budget=1)
        self.assertIn("/gazelle.yml/", read.call_args.args[0])
        self.assertEqual((state["cursor"], state["requestsUsed"], state["status"]), (3, 1, "ok"))


if __name__ == "__main__":
    unittest.main()
