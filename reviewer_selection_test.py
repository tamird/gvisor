import unittest
from datetime import datetime, timezone

from reviewer_selection import (get_reviewer_candidates, needs_reviewer_paths, recent_reviewer_work,
                                reviewer_governance, reviewer_history)


class ReviewerSelectionTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 9, 19, tzinfo=timezone.utc)
        self.path = "pkg/sentry/platform/systrap/sysmsg/BUILD"
        self.pr = {"number": 2, "repo": "google/gvisor", "authorLogin": "author", "files": [self.path]}
        self.work = {
            "number": 1, "repo": "google/gvisor", "url": "prior", "authorLogin": "author",
            "files": [self.path], "status": "open", "reviewRequests": [{"login": "casey"}],
            "reviewTimeline": {"items": [{"kind": "request", "at": "2026-10-25T17:00:00Z",
                                           "reviewer": {"login": "casey"}}]},
            "feedback": {"items": [
                {"kind": "comment", "createdAt": "2026-11-02T17:00:00Z", "author": {"login": "author"},
                 "body": "@casey, could you review the offsets?"},
                {"kind": "inline", "createdAt": "2026-11-02T18:00:00Z", "author": {"login": "casey"},
                 "path": self.path, "body": "Check how undefined symbols are handled.", "url": "review"},
            ]},
        }

    def picks(self, **kwargs):
        history = reviewer_history([self.work], "author", maintainers={"casey", "former"})
        return get_reviewer_candidates(self.pr, history, kwargs.get("previous", set()),
                                       kwargs.get("requested", set()), kwargs.get("now", self.now))

    def test_related_response(self):
        candidate, = self.picks()
        self.assertEqual((candidate["login"], candidate["channel"], candidate["evidence"]),
                         ("casey", "comment", "review"))
        self.assertEqual(candidate["successfulResponses"], {"comment": 1})
        self.assertEqual(candidate["pending"], 1)

    def test_rotation_and_recent_activity(self):
        self.assertEqual(self.picks(previous={"Casey"}), [])
        self.assertEqual(self.picks(now="2027-01-01T00:00:00Z"), [])

    def test_assignment_alone_does_not_establish_expertise(self):
        self.pr["files"] = ["pkg/unrelated/BUILD"]
        self.assertEqual(self.picks(requested={"casey"}), [])

    def test_only_unassigned_current_sources_trigger_path_history(self):
        source = {**self.pr, "status": "open", "head": "current", "files": ["Makefile", "tools/bazel.mk"]}
        row = {"status": "needs-reviewer", "head": "current"}
        self.assertEqual(needs_reviewer_paths([source], {"2": row}), {"tools/bazel.mk"})
        for change in ({"status": "merged"}, {"head": "old"}, {"repo": "other/project"}):
            with self.subTest(change=change):
                self.assertEqual(needs_reviewer_paths([{**source, **change}], {"2": row}), set())
        self.assertEqual(needs_reviewer_paths([source], {"2": {**row, "status": "waiting"}}), set())

    def test_authors_of_unassigned_file_include_copybara_commits(self):
        path = "tools/make/bazel.mk"
        oid = "a" * 40
        url = f"https://github.com/google/gvisor/commit/{oid}"
        def commit(login, sha, parents=1):
            return {"oid": sha, "url": f"https://github.com/google/gvisor/commit/{sha}",
                    "author": {"user": {"login": login}}, "parents": {"totalCount": parents}}

        def graphql(query):
            self.assertIn(f'history(first:20, path:"{path}")', query)
            self.assertIn('defaultBranchRef', query)
            self.assertNotIn('search(type:ISSUE', query)
            commits = [commit("casey", "b" * 40, 2), commit("former", "c" * 40), commit("casey", oid),
                       commit("casey", "d" * 40)]
            return {"a0": {"nodes": []}, "r0": {"nodes": []},
                    "repository": {"defaultBranchRef": {"target": {"p0": {"nodes": commits}}}}}

        previous = {"checkedAt": self.now.isoformat(), "logins": ["casey"], "observations": [], "authored": []}
        found = recent_reviewer_work(graphql, {"casey"}, previous, self.now, paths={path})
        self.assertEqual(found["authored"], [{"login": "casey", "commit": oid, "url": url, "paths": [path]}])
        self.assertIs(recent_reviewer_work(self.fail, {"casey"}, found, self.now, paths={path}), found)
        self.pr["files"] = [path]
        self.work["feedback"]["items"][-1].update(kind="review", path=None, body="", state="APPROVED")
        self.assertEqual(self.picks(), [])
        history = reviewer_history([self.work], "author", maintainers={"casey"}, authored=found["authored"])
        candidate, = get_reviewer_candidates(self.pr, history, set(), set(), self.now)
        self.assertEqual((candidate["login"], candidate["scope"], candidate["evidence"]), ("casey", path, url))
        self.pr["files"] = ["tools/make/unrelated.mk"]
        self.assertEqual(get_reviewer_candidates(self.pr, history, set(), set(), self.now), [])

    def test_roster_sample_and_pending_path_batches_use_separate_requests(self):
        calls = []
        def graphql(query):
            calls.append(query)
            if 'search(type:ISSUE' in query:
                self.assertNotIn('history(first:', query)
                return {"a0": {"nodes": []}, "r0": {"nodes": []}}
            return {"repository": {"defaultBranchRef": {"target": {
                f"p{i}": {"nodes": []} for i in range(query.count("history(first:"))}}}}

        paths = {f"tools/input{i:02}" for i in range(13)}
        first = recent_reviewer_work(graphql, {"casey"}, {}, self.now, paths=paths)
        second = recent_reviewer_work(graphql, {"casey"}, first, self.now, paths=paths)
        last = recent_reviewer_work(graphql, {"casey"}, second, self.now, paths=paths)
        self.assertEqual([call.count("history(first:") for call in calls], [0, 12, 1])
        self.assertEqual(set(last["checkedPaths"]), paths)

    def test_automated_review_on_human_account(self):
        self.work["feedback"]["items"][-1]["body"] = "[🤖🐸🤖] Review agent has no findings."
        self.assertEqual(self.picks(), [])

    def test_multifile_review_does_not_imply_expertise_in_every_file(self):
        item = self.work["feedback"]["items"][-1]
        item["kind"] = "review"
        del item["path"]
        self.work["files"].append("runsc/cmd/run.go")
        self.assertEqual(self.picks(), [])

    def test_active_governance(self):
        active, paths = reviewer_governance(
            "areas: [{name: systrap, paths: [/pkg/sentry/platform/systrap]}]",
            "maintainers: [{github: Casey, status: ACTIVE, areas: [systrap]}, "
            "{github: former, status: HIATUS_SINCE:2026-01-01, areas: [systrap]}, "
            "{github: general, status: ACTIVE}]")
        self.assertEqual(active, {"casey", "general"})
        self.assertEqual(paths, {"pkg/sentry/platform/systrap": {"casey"}})
