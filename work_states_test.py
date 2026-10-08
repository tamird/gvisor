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
        self.assertEqual(set(pr_states(original())), {"review"})
        self.assertEqual(set(pr_states(original(status="draft"))), {"draft"})
        self.assertEqual(set(pr_states(original(reviewDecision="CHANGES_REQUESTED"))), {"changes-requested"})
        self.assertEqual(set(pr_states(original(status="draft", reviewDecision="APPROVED"))), {"draft"})

    def test_waiting_import_keeps_unknown_checks_visible(self):
        approved = original(reviewDecision="APPROVED")
        self.assertEqual(pr_states(approved)["awaiting-import"]["qualifier"], "Checks unknown")
        self.assertNotIn("awaiting-import", pr_states({**approved, "importsComplete": False}))
        labelled = original(labels=["ready to pull"])
        self.assertEqual(set(pr_states(labelled)), {"review", "awaiting-import"})
        self.assertNotIn("awaiting-import", pr_states({**labelled, "labelsComplete": False}))

    def test_only_current_active_import_failure_counts(self):
        imported = original(number=2, head="b" * 40, matchesSourceHead=False,
                            checks={"state": "FAILURE", "contexts": [{"state": "FAILURE"}], "complete": True})
        pr = original(reviewDecision="APPROVED", imports=[imported])
        self.assertEqual(set(pr_states(pr)), {"awaiting-import"})
        imported["matchesSourceHead"] = True
        self.assertEqual(set(pr_states(pr)), {"failing", "importing"})
        imported["status"] = "closed"
        self.assertEqual(set(pr_states(pr)), {"awaiting-import"})
        imported["status"] = "merged"
        self.assertEqual(set(pr_states(pr)), {"imported"})

    def test_failure_and_pending_overlap(self):
        pr = original(checks={"state": "PENDING", "contexts": [{"state": "FAILURE"}, {"state": "PENDING"}]})
        self.assertEqual(set(pr_states(pr)), {"review", "failing", "checks-pending"})

    def test_same_head_age_survives_refresh_but_not_state_or_head_change(self):
        registry = {"nodes": []}
        pr = original()
        first = {"checkedAt": "2026-10-01T00:00:00Z", "prs": [copy.deepcopy(pr)]}
        second = record_states([pr], registry, first, "2026-10-02T00:00:00Z")
        self.assertEqual(second["pr:1"]["review"]["since"], first["checkedAt"])
        previous = {"checkedAt": "2026-10-02T00:00:00Z", "prs": [copy.deepcopy(pr)], "workStates": second}
        pr["updatedAt"] = "2026-10-03T00:00:00Z"
        self.assertEqual(record_states([pr], registry, previous, "2026-10-03T00:00:00Z"), second)
        pr["head"] = "c" * 40
        self.assertEqual(record_states([pr], registry, previous, "2026-10-03T00:00:00Z")["pr:1"]["review"]["since"], "2026-10-03T00:00:00Z")
        previous = {"checkedAt": "2026-10-03T00:00:00Z", "prs": [original(reviewDecision="APPROVED")], "workStates": {"pr:1": {}}}
        self.assertEqual(record_states([original()], registry, previous, "2026-10-04T00:00:00Z")["pr:1"]["review"]["since"], "2026-10-04T00:00:00Z")

    def test_exact_close_timestamp_and_curated_head(self):
        branch = {"id": "branch:other", "type": "branch", "ref": "other", "head": "b" * 40,
                  "status": "contributor review requested"}
        registry = {"nodes": [branch]}
        pr = original(status="merged", mergedAt="2026-10-01T01:00:00Z")
        states = record_states([pr], registry, {}, "2026-10-02T00:00:00Z")
        self.assertEqual(states["pr:1"]["merged"]["basis"], "transition")
        self.assertEqual(states["pr:1"]["merged"]["since"], pr["mergedAt"])
        self.assertEqual(states["branch:other"]["review"]["scope"], branch["head"])
        del branch["head"]
        unknown = record_states([pr], registry, {}, "2026-10-02T00:00:00Z")["branch:other"]["review"]
        self.assertEqual((unknown["basis"], unknown["since"], unknown["scope"]), ("unknown", None, None))
        branch["ref"] = "topic"
        self.assertNotIn("branch:other", record_states([pr], registry, {}, "2026-10-02T00:00:00Z"))

if __name__ == "__main__":
    unittest.main()
