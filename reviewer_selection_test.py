import unittest
from datetime import datetime, timezone

from reviewer_selection import get_reviewer_candidates, reviewer_governance, reviewer_history


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
