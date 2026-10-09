"""Regressions for review/import classification and observation continuity."""

import copy
import unittest

from work_states import check_purpose, checks_passed, pr_states, record_states, remember_mergeability, review_decision


def original(**changes: object) -> dict:
    pr = {"number": 1, "status": "open", "head": "a" * 40, "ref": "topic",
          "reviewDecision": None, "imports": [], "importsComplete": True,
          "labels": [], "labelsComplete": True, "checks": None}
    if changes.get("reviewDecision") == "APPROVED":
        pr.update(reviewsComplete=True, reviewRequestsComplete=True, reviewRequests=[],
                  feedback={"items": [{"kind": "review", "state": "APPROVED", "submittedAt": "2026-09-01T00:00:00Z",
                                       "author": {"login": "reviewer"}, "commit": {"oid": changes.get("head", pr["head"])}}]})
    pr.update(changes)
    return pr


class WorkStatesTest(unittest.TestCase):
    def test_assignment_error_is_not_a_source_failure(self):
        assignment = {"kind": "check-run", "appSlug": "github-actions", "workflow": "Auto Assign",
                      "name": "assign", "state": "FAILURE", "purpose": "reviewer-assignment"}
        lint = {"name": "lint", "state": "SUCCESS", "purpose": "validation"}
        pr = original(checks={"state": "FAILURE", "total": 2, "complete": True,
                              "contexts": [assignment, lint]})
        self.assertEqual(set(pr_states(pr)), {"maintainer-review"})
        self.assertEqual(pr["checks"]["state"], "FAILURE")
        self.assertFalse(checks_passed(pr))
        previous = {"checkedAt": "2026-10-01T00:00:00Z", "prs": [copy.deepcopy(pr)],
                    "workStates": {"pr:1": {"failing": {"scope": "1:" + pr["head"],
                        "since": "2026-10-01T00:00:00Z", "basis": "observed", "qualifier": "Source checks failing"}}}}
        self.assertNotIn("failing", record_states([pr], {"nodes": []}, previous, "2026-10-02T00:00:00Z")["pr:1"])
        pr["checks"]["contexts"][1]["state"] = "FAILURE"
        self.assertEqual(pr_states(pr)["failing"]["qualifier"], "Source checks failing")

    def test_administrative_identity_requires_provider_and_workflow(self):
        check = {"kind": "check-run", "appSlug": "github-actions", "workflow": "Auto Assign", "name": "assign"}
        self.assertEqual(check_purpose(check), "reviewer-assignment")
        for change in ({"kind": "status"}, {"appSlug": "other"}, {"workflow": None},
                       {"workflow": "Build"}, {"name": "lint"}):
            with self.subTest(change=change):
                self.assertEqual(check_purpose({**check, **change}), "validation")

    def test_admin_only_and_incomplete_checks_do_not_grant_readiness(self):
        assignment = {"state": "FAILURE", "purpose": "reviewer-assignment"}
        for complete, total, contexts in [(True, 1, [assignment]),
                                         (False, 2, [assignment, {"state": "SUCCESS"}])]:
            with self.subTest(complete=complete):
                pr = original(reviewDecision="APPROVED", mergeable="MERGEABLE",
                              checks={"state": "FAILURE", "complete": complete, "total": total, "contexts": contexts})
                self.assertFalse(checks_passed(pr))
                self.assertEqual("failing" in pr_states(pr), not complete)
        unknown = original(checks={"state": "FAILURE", "complete": True, "total": 1,
                                   "contexts": [{"name": "assign", "state": "FAILURE"}]})
        self.assertIn("failing", pr_states(unknown))

    def test_unexplained_aggregate_failure_and_pending_are_retained(self):
        for aggregate in ("FAILURE", "PENDING"):
            for assignment in ([], [{"state": "SUCCESS", "purpose": "reviewer-assignment"}]):
                with self.subTest(aggregate=aggregate, assignment=assignment):
                    contexts = [{"name": "lint", "state": "SUCCESS"}, *assignment]
                    pr = original(checks={"state": aggregate, "complete": True,
                                          "total": len(contexts), "contexts": contexts})
                    self.assertIn("failing" if aggregate == "FAILURE" else "checks-pending", pr_states(pr))
                    self.assertFalse(checks_passed(pr))

    def test_import_validation_failure_survives_source_assignment_error(self):
        imported = original(number=2, head="b" * 40, matchesSourceHead=True,
                            checks={"state": "FAILURE", "complete": True, "total": 1,
                                    "contexts": [{"name": "buildkite/pipeline", "state": "FAILURE"}]})
        pr = original(reviewDecision="APPROVED", imports=[imported],
                      checks={"state": "FAILURE", "complete": True, "total": 2,
                              "contexts": [{"state": "FAILURE", "purpose": "reviewer-assignment"},
                                           {"state": "SUCCESS", "purpose": "validation"}]})
        self.assertEqual(pr_states(pr)["failing"]["qualifier"], "Import #2 failing")

    def test_unknown_retains_concrete_conflict_without_approval_credit(self):
        old = original(mergeable="CONFLICTING", base="b" * 40, checkedAt="2026-10-01T00:00:00Z")
        pr = {**old, "mergeable": "UNKNOWN", "base": "c" * 40}
        observed = remember_mergeability(pr, old, "2026-10-02T00:00:00Z")
        self.assertEqual(observed, {"state": "CONFLICTING", "head": old["head"], "base": old["base"], "checkedAt": old["checkedAt"]})
        pr["mergeabilityObservation"] = observed
        self.assertIn("Last observed conflict", pr_states(pr)["conflicts"]["qualifier"])
        self.assertNotIn("waiting-merge", pr_states(pr))
        self.assertEqual(remember_mergeability(pr, pr, "2026-10-03T00:00:00Z"), observed)
        self.assertNotIn("conflicts", pr_states({**pr, "head": "d" * 40}))
        clear = {**pr, "mergeable": "MERGEABLE"}
        self.assertNotIn("conflicts", pr_states(clear))
        self.assertEqual(remember_mergeability(clear, pr, "2026-10-03T00:00:00Z")["state"], "MERGEABLE")
        self.assertNotIn("conflicts", pr_states(original(mergeable="UNKNOWN")))

    def test_approval_must_match_current_head_and_latest_review(self):
        pr = original(reviewDecision="APPROVED")
        self.assertEqual(review_decision(pr), "APPROVED")
        pr["head"] = "b" * 40
        self.assertEqual(set(pr_states(pr)), {"maintainer-review"})
        self.assertEqual(set(pr_states({**pr, "labels": ["ready to pull"]})), {"maintainer-review"})
        pr["feedback"]["items"][0]["commit"]["oid"] = pr["head"]
        self.assertEqual(review_decision(pr), "APPROVED")
        approval = pr["feedback"]["items"][0]
        comment = {**approval, "state": "COMMENTED", "submittedAt": "2026-10-01T00:00:00Z"}
        pr["feedback"]["items"].append(comment)
        self.assertEqual(review_decision(pr), "APPROVED")
        for change in ({"state": "DISMISSED"}, {"state": "CHANGES_REQUESTED"},
                       {"state": "APPROVED", "commit": None}):
            with self.subTest(change=change):
                pr["feedback"]["items"][-1] = {**comment, **change}
                self.assertEqual(review_decision(pr), "REVIEW_REQUIRED")
        pr["feedback"]["items"] = [approval]
        for change in ({"reviewsComplete": False}, {"reviewRequestsComplete": False},
                       {"reviewRequests": [{"login": "reviewer"}]},
                       {"feedback": {"items": [{**approval, "commit": None}]}}):
            with self.subTest(change=change):
                self.assertEqual(review_decision({**pr, **change}), "REVIEW_REQUIRED")
        self.assertEqual(review_decision({**pr, "githubReviewDecision": "REVIEW_REQUIRED"}), "REVIEW_REQUIRED")

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
        self.assertEqual(set(pr_states(labelled)), {"maintainer-review"})
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

    def test_contributor_changes_are_current_head_branch_evidence(self):
        branch = {"id": "branch:revision", "type": "branch", "ref": "revision",
                  "head": "b" * 40,
                  "contributorChangesRequested": {"head": "b" * 40}}
        registry = {"nodes": [branch]}
        states = record_states([original()], registry, {}, "2026-10-02T00:00:00Z")
        self.assertEqual(set(states[branch["id"]]), {"changes-requested"})
        self.assertEqual(states[branch["id"]]["changes-requested"]["qualifier"], "Contributor")
        self.assertNotIn("changes-requested", states["pr:1"])
        branch["head"] = "c" * 40
        self.assertNotIn(branch["id"], record_states([], registry, {}, "2026-10-03T00:00:00Z"))
        del branch["head"]
        self.assertNotIn(branch["id"], record_states([], registry, {}, "2026-10-03T00:00:00Z"))

    def test_answered_contributor_question_does_not_keep_pending_age(self):
        branch = {"id": "branch:revision", "type": "branch", "ref": "revision",
                  "head": "b" * 40,
                  "contributorChangesRequested": {"head": "b" * 40}}
        previous = {"checkedAt": "2026-10-01T00:00:00Z", "prs": [], "workStates": {
            branch["id"]: {"contributor-review": {
                "scope": branch["head"], "since": "2026-09-01T00:00:00Z", "basis": "observed"}}}}
        current = record_states([], {"nodes": [branch]}, previous, "2026-10-02T00:00:00Z")
        self.assertEqual(set(current[branch["id"]]), {"changes-requested"})
        self.assertEqual(current[branch["id"]]["changes-requested"]["since"], "2026-10-02T00:00:00Z")
        branch["ref"] = "topic"
        self.assertNotIn(branch["id"], record_states([original()], {"nodes": [branch]}, previous,
                                                    "2026-10-03T00:00:00Z"))

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

    def test_contributor_amendment_belongs_to_its_own_branch(self):
        node = {"id": "pr:1", "type": "pr", "status": "open",
                "contributorReview": {"status": "pending", "head": "c" * 40}}
        branch = {"id": "branch:amendment", "type": "branch", "ref": "review/amendment",
                  "contributorReview": copy.deepcopy(node["contributorReview"])}
        registry = {"nodes": [node, branch]}
        pr = original(reviewDecision="APPROVED")
        previous = {"checkedAt": "2026-10-01T00:00:00Z", "prs": [pr],
                    "workStates": {"pr:1": {"review": {"scope": "c" * 40, "basis": "observed", "since": "2026-09-01T00:00:00Z"}}}}
        result = record_states([pr], registry, previous, "2026-10-02T00:00:00Z")
        self.assertNotIn("contributor-review", result["pr:1"])
        self.assertEqual(result["branch:amendment"]["contributor-review"]["scope"], "c" * 40)
        self.assertEqual(result["branch:amendment"]["contributor-review"]["since"], "2026-10-02T00:00:00Z")
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

    def test_new_head_rule_does_not_backdate_changed_review_state(self):
        pr = original(reviewDecision="APPROVED")
        pr["head"] = "c" * 40
        previous = {"checkedAt": "2026-10-01T00:00:00Z", "prs": [pr], "workStates": {"pr:1": {}}}
        recorded = record_states([pr], {"nodes": []}, previous, "2026-10-02T00:00:00Z")
        self.assertEqual(recorded["pr:1"]["maintainer-review"]["since"], "2026-10-02T00:00:00Z")

    def test_prepared_proposal_is_not_a_review_request(self):
        branch = {"id": "branch:proposal", "type": "branch", "ref": "review/proposal",
                  "head": "b" * 40, "status": "Prepared proposal"}
        pr = original(reviewDecision="APPROVED")
        result = record_states([pr], {"nodes": [branch]}, {}, "2026-10-02T00:00:00Z")
        self.assertNotIn("branch:proposal", result)
        self.assertEqual(set(result["pr:1"]), {"awaiting-import"})

if __name__ == "__main__":
    unittest.main()
