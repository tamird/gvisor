"""Regressions for review/import classification and observation continuity."""

import copy
import unittest

from work_states import pr_states, record_states


def original(**changes: object) -> dict:
    pr = {"number": 1, "status": "open", "head": "a" * 40, "ref": "topic",
          "reviewDecision": None, "imports": [], "importsComplete": True,
          "labels": [], "labelsComplete": True, "checks": None}
    pr.update(changes)
    return pr


class WorkStatesTest(unittest.TestCase):
    def test_review_draft_and_changes_are_distinct(self):
        self.assertEqual(set(pr_states(original())), {"maintainer-review"})
        self.assertEqual(set(pr_states(original(status="draft"))), {"draft"})
        self.assertEqual(set(pr_states(original(reviewDecision="CHANGES_REQUESTED"))), {"changes-requested"})
        self.assertEqual(set(pr_states(original(status="draft", reviewDecision="APPROVED"))), {"draft"})

    def test_waiting_import_keeps_unknown_checks_visible(self):
        approved = original(reviewDecision="APPROVED")
        self.assertEqual(pr_states(approved)["awaiting-import"]["qualifier"], "Checks unknown")
        self.assertNotIn("awaiting-import", pr_states({**approved, "importsComplete": False}))
        labelled = original(labels=["ready to pull"])
        self.assertEqual(set(pr_states(labelled)), {"maintainer-review", "awaiting-import"})
        self.assertNotIn("awaiting-import", pr_states({**labelled, "labelsComplete": False}))

    def test_only_current_active_import_failure_counts(self):
        imported = original(number=2, head="b" * 40, matchesSourceHead=False,
                            checks={"state": "FAILURE", "contexts": [{"state": "FAILURE"}], "complete": True})
        pr = original(reviewDecision="APPROVED", imports=[imported])
        self.assertEqual(set(pr_states(pr)), {"awaiting-import"})
        imported["matchesSourceHead"] = True
        self.assertEqual(set(pr_states(pr)), {"failing", "importing"})
        self.assertEqual(pr_states(pr)["failing"]["qualifier"], "Import #2 failing")
        imported["status"] = "closed"
        self.assertEqual(set(pr_states(pr)), {"awaiting-import"})
        imported["status"] = "merged"
        self.assertEqual(set(pr_states(pr)), {"imported"})

    def test_failure_and_pending_overlap(self):
        pr = original(checks={"state": "PENDING", "contexts": [{"state": "FAILURE"}, {"state": "PENDING"}]})
        self.assertEqual(set(pr_states(pr)), {"maintainer-review", "failing", "checks-pending"})

    def test_same_head_age_survives_refresh_but_not_state_or_head_change(self):
        registry = {"nodes": []}
        pr = original()
        first = {"checkedAt": "2026-10-01T00:00:00Z", "prs": [copy.deepcopy(pr)]}
        second = record_states([pr], registry, first, "2026-10-02T00:00:00Z")
        self.assertEqual(second["pr:1"]["maintainer-review"]["since"], first["checkedAt"])
        previous = {"checkedAt": "2026-10-02T00:00:00Z", "prs": [copy.deepcopy(pr)], "workStates": second}
        pr["updatedAt"] = "2026-10-03T00:00:00Z"
        self.assertEqual(record_states([pr], registry, previous, "2026-10-03T00:00:00Z"), second)
        pr["head"] = "c" * 40
        self.assertEqual(record_states([pr], registry, previous, "2026-10-03T00:00:00Z")["pr:1"]["maintainer-review"]["since"], "2026-10-03T00:00:00Z")
        previous = {"checkedAt": "2026-10-03T00:00:00Z", "prs": [original(reviewDecision="APPROVED")], "workStates": {"pr:1": {}}}
        self.assertEqual(record_states([original()], registry, previous, "2026-10-04T00:00:00Z")["pr:1"]["maintainer-review"]["since"], "2026-10-04T00:00:00Z")

    def test_exact_close_timestamp_and_curated_head(self):
        branch = {"id": "branch:other", "type": "branch", "ref": "other", "head": "b" * 40,
                  "status": "contributor review requested",
                  "contributorReview": {"status": "pending", "head": "b" * 40}}
        registry = {"nodes": [branch]}
        pr = original(status="merged", mergedAt="2026-10-01T01:00:00Z")
        states = record_states([pr], registry, {}, "2026-10-02T00:00:00Z")
        self.assertEqual(states["pr:1"]["merged"]["basis"], "transition")
        self.assertEqual(states["pr:1"]["merged"]["since"], pr["mergedAt"])
        self.assertEqual(states["branch:other"]["contributor-review"]["scope"], branch["head"])
        branch["contributorReview"]["head"] = None
        unknown = record_states([pr], registry, {}, "2026-10-02T00:00:00Z")["branch:other"]["contributor-review"]
        self.assertEqual((unknown["basis"], unknown["since"], unknown["scope"]), ("unknown", None, None))
        branch["ref"] = "topic"
        self.assertNotIn("branch:other", record_states([pr], registry, {}, "2026-10-02T00:00:00Z"))

    def test_waiting_merge_requires_verified_complete_green_heads(self):
        green = {"state": "SUCCESS", "total": 1, "contexts": [{"state": "SUCCESS"}], "complete": True}
        imported = original(number=2, head="b" * 40, matchesSourceHead=True,
                            mergeable="MERGEABLE", checks=green)
        pr = original(reviewDecision="APPROVED", mergeable="MERGEABLE", checks=green, imports=[imported])
        self.assertEqual(set(pr_states(pr)), {"importing", "waiting-merge"})
        for change in ({"status": "draft"}, {"mergeable": "UNKNOWN"}, {"mergeable": "CONFLICTING"},
                       {"reviewDecision": "CHANGES_REQUESTED"}, {"checks": None},
                       {"checks": {**green, "complete": False}}, {"checks": {**green, "total": 0, "contexts": []}},
                       {"checks": {**green, "contexts": [{"state": "FAILURE"}]}}):
            with self.subTest(change=change):
                candidate = {**pr, "imports": [{**imported, **change}]}
                self.assertIn("importing", pr_states(candidate))
                self.assertNotIn("waiting-merge", pr_states(candidate))
        self.assertNotIn("waiting-merge", pr_states({**pr, "mergeable": "UNKNOWN"}))
        incomplete = pr_states({**pr, "importsComplete": False})
        self.assertIn("importing", incomplete)
        self.assertNotIn("waiting-merge", incomplete)
        self.assertNotIn("waiting-merge", pr_states({**pr, "reviewDecision": "CHANGES_REQUESTED"}))
        self.assertNotIn("waiting-merge", pr_states({**pr, "checks": None}))

    def test_contributor_amendment_is_independent_of_maintainer_approval(self):
        node = {"id": "pr:1", "type": "pr", "status": "open",
                "contributorReview": {"status": "pending", "head": "c" * 40}}
        registry = {"nodes": [node]}
        pr = original(reviewDecision="APPROVED")
        previous = {"checkedAt": "2026-10-01T00:00:00Z", "prs": [pr],
                    "workStates": {"pr:1": {"review": {"scope": "c" * 40, "basis": "observed", "since": "2026-09-01T00:00:00Z"}}}}
        result = record_states([pr], registry, previous, "2026-10-02T00:00:00Z")
        self.assertEqual(result["pr:1"]["contributor-review"]["scope"], "c" * 40)
        self.assertEqual(result["pr:1"]["contributor-review"]["since"], "2026-10-02T00:00:00Z")
        self.assertNotIn("maintainer-review", result["pr:1"])
        del node["contributorReview"]
        self.assertNotIn("contributor-review", record_states([pr], registry, previous, "2026-10-02T00:00:00Z")["pr:1"])
        node["status"] = "contributor review requested"
        self.assertNotIn("contributor-review", record_states([pr], registry, previous, "2026-10-02T00:00:00Z")["pr:1"])

    def test_legacy_review_age_migration_keeps_authorities_separate(self):
        pr = original()
        branch = {"id": "branch:other", "type": "branch", "ref": "other",
                  "contributorReview": {"status": "pending", "head": "b" * 40}}
        old = {"scope": pr["head"], "since": "2026-09-01T00:00:00Z", "basis": "observed"}
        previous = {"checkedAt": "2026-10-01T00:00:00Z", "prs": [pr], "workStates": {
            "pr:1": {"review": old}, "branch:other": {"review": {**old, "scope": "b" * 40}}}}
        result = record_states([pr], {"nodes": [branch]}, previous, "2026-10-02T00:00:00Z")
        self.assertEqual(result["pr:1"]["maintainer-review"]["since"], old["since"])
        self.assertEqual(result["branch:other"]["contributor-review"]["since"], old["since"])
        branch["contributorReview"]["head"] = "c" * 40
        result = record_states([pr], {"nodes": [branch]}, previous, "2026-10-02T00:00:00Z")
        self.assertEqual(result["branch:other"]["contributor-review"]["since"], "2026-10-02T00:00:00Z")

if __name__ == "__main__":
    unittest.main()
