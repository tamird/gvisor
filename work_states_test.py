"""Regressions for review/import classification and observation continuity."""

import copy
from importlib import import_module
import subprocess
import unittest
from unittest.mock import patch

from work_states import approved_patch, check_purpose, check_qualifier, checks_passed, pr_states, public_import_disposition, record_states, remember_mergeability, review_decision


def original(**changes: object) -> dict:
    pr = {"number": 1, "status": "open", "head": "a" * 40, "ref": "topic", "headOwner": "tamird", "headRepo": "tamird/gvisor",
          "reviewDecision": None, "headHistoryComplete": True, "headCommittedAt": "2026-08-31T00:00:00Z",
          "imports": [], "importsComplete": True, "sourceHead": "a" * 40,
          "labels": [], "labelsComplete": True, "checks": None}
    if changes.get("reviewDecision") == "APPROVED":
        pr.update(reviewsComplete=True, reviewRequestsComplete=True, reviewRequests=[],
                  feedback={"items": [{"kind": "review", "state": "APPROVED", "submittedAt": "2026-09-01T00:00:00Z",
                                       "author": {"login": "reviewer"}, "commit": {"oid": changes.get("head", pr["head"])}}]})
    pr.update(changes)
    return pr


class WorkStatesTest(unittest.TestCase):
    def test_branch_mismatch_cannot_inherit_pr_readiness(self):
        pr = original(reviewDecision="APPROVED", mergeable="MERGEABLE")
        self.assertIn("awaiting-import", pr_states(pr))
        pr["branchHead"] = "b" * 40
        self.assertNotIn("awaiting-import", pr_states(pr))
        pr["branchHead"] = pr["head"]
        self.assertIn("awaiting-import", pr_states(pr))

    def test_optional_transport_failure_preserves_primary_observations(self):
        # Use the normal module finder for the collector's CLI filename.
        collector = import_module("update-status")
        primary = {"number": 1, "state": "OPEN", "mergeable": "UNKNOWN",
                   "headRefOid": "a" * 40, "baseRefOid": "b" * 40,
                   "mergeStateStatus": "UNKNOWN", "_retrievedAt": "2026-10-14T20:00:00Z"}
        before = copy.deepcopy(primary)
        failure = subprocess.CompletedProcess([], 1, stdout="", stderr="gh: HTTP 502")
        with patch.object(collector, "requests", 0), patch.object(collector.subprocess, "run", return_value=failure) as calls:
            self.assertEqual(collector.recheck_unknown_mergeability([primary]),
                             {"status": "unavailable", "numbers": [1]})
            self.assertEqual(primary, before)
            with self.assertRaises(collector.GitHubTransportError):
                collector.fetch_numbers([1], timeline=False)
            self.assertEqual(calls.call_count, 4)

    def test_registry_decision_does_not_inherit_older_github_clock(self):
        full_at = "2026-10-12T11:48:00Z"
        decision_at = "2026-10-12T14:41:00Z"
        branch = {"id": "branch:proposal", "type": "branch", "ref": "proposal",
                  "head": "b" * 40,
                  "contributorReview": {"status": "pending", "head": "b" * 40}}
        registry = {"meta": {"repo": "google/gvisor", "updatedAt": decision_at}, "nodes": [branch]}
        pr = original(checkedAt=full_at)
        states = record_states([pr], registry, {}, full_at)
        decision = states[branch["id"]]["contributor-review"]
        self.assertEqual((decision["since"], decision["observedAt"]), (decision_at, decision_at))
        self.assertEqual(states["pr:1"]["maintainer-review"]["observedAt"], full_at)
        registry["meta"]["updatedAt"] = "2026-10-12T15:00:00Z"
        later = record_states([pr], registry, {"workStates": states}, full_at)
        self.assertEqual(later[branch["id"]]["contributor-review"]["since"], decision_at)
        self.assertEqual(later["pr:1"]["maintainer-review"]["observedAt"], full_at)

    def test_direct_terminal_observation_preserves_exact_transition(self):
        full_at = "2026-10-12T11:48:00Z"
        merged_at = "2026-10-12T17:54:16Z"
        observed_at = "2026-10-12T18:05:00+00:00"
        pr = original(status="merged", mergedAt=merged_at, checkedAt=full_at,
                      statusObservation={"checkedAt": observed_at})
        state = record_states([pr], {"meta": {"repo": "google/gvisor"}, "nodes": []}, {}, full_at)["pr:1"]["merged"]
        self.assertEqual((state["since"], state["basis"], state["observedAt"]),
                         (merged_at, "transition", observed_at))
        pr["checkedAt"] = "2026-10-12T18:36:00Z"
        self.assertEqual(record_states([pr], {"meta": {"repo": "google/gvisor"}, "nodes": []}, {}, full_at)["pr:1"]["merged"]["observedAt"],
                         pr["checkedAt"])

    def test_legacy_first_observation_uses_pr_time(self):
        pr = original(checkedAt="2026-10-12T12:00:00Z")
        previous = {"checkedAt": "2026-10-12T11:00:00Z", "prs": [copy.deepcopy(pr)]}
        pr["checkedAt"] = "2026-10-12T13:00:00Z"
        state = record_states([pr], {"meta": {"repo": "google/gvisor"}, "nodes": []}, previous, pr["checkedAt"])["pr:1"]["maintainer-review"]
        self.assertEqual((state["since"], state["observedAt"]),
                         ("2026-10-12T12:00:00Z", "2026-10-12T13:00:00Z"))

    def test_reassociated_old_approval_is_not_a_fresh_submission(self):
        pr = original(reviewDecision="APPROVED", headCommittedAt="2026-10-09T19:31:21Z",
                      headIntroducedAt="2026-10-09T19:32:12Z")
        approval = pr["feedback"]["items"][0]
        approval.update(submittedAt="2026-10-06T20:18:13Z", updatedAt="2026-10-09T19:32:12Z")
        self.assertEqual(review_decision(pr), "REVIEW_REQUIRED")
        self.assertNotIn("awaiting-import", pr_states(pr))
        approval["submittedAt"] = "2026-10-09T19:32:00Z"
        self.assertEqual(review_decision(pr), "REVIEW_REQUIRED")
        approval["submittedAt"] = "2026-10-09T19:33:00Z"
        self.assertEqual(review_decision(pr), "APPROVED")
        del pr["headIntroducedAt"]
        approval["submittedAt"] = "2026-10-06T20:18:13Z"
        self.assertEqual(review_decision(pr), "REVIEW_REQUIRED")

    def test_equivalent_approved_patch_routes_without_fresh_review(self):
        pr = original(repo="google/gvisor", githubReviewDecision="REVIEW_REQUIRED", reviewDecision="REVIEW_REQUIRED",
                      headCommittedAt="2026-10-19T18:00:00Z", reviewsComplete=True, reviewRequestsComplete=True,
                      reviewRequests=[], threads={"complete": True, "unresolved": 0},
                      feedback={"complete": True, "items": [{"kind": "review", "id": "review-1", "state": "APPROVED",
                                "submittedAt": "2026-10-09T12:00:00Z", "author": {"login": "reviewer"},
                                "commit": {"oid": "b" * 40}}]})
        proof = {"head": pr["head"], "approvedHead": "b" * 40, "reviewId": "review-1", "reviewer": "reviewer",
                 "submittedAt": "2026-10-09T12:00:00Z", "verifiedAt": "2026-10-19T19:00:00Z",
                 "method": "stable-patch-id", "oldPatchId": "c" * 40, "newPatchId": "c" * 40}
        pr["checkedAt"] = "2026-10-19T18:30:00Z"
        registry = {"meta": {"repo": "google/gvisor"}, "nodes": [{"id": "pr:1", "type": "pr", "approvedPatch": proof}]}
        pending = {**pr, "checks": {"state": "PENDING", "total": 1, "complete": True,
                                    "contexts": [{"name": "test", "state": "PENDING"}]}}
        observed = record_states([pending], registry, {}, pr["checkedAt"])["pr:1"]
        self.assertEqual(observed["awaiting-import"]["observedAt"], proof["verifiedAt"])
        self.assertEqual(observed["checks-pending"]["observedAt"], pr["checkedAt"])
        raw = copy.deepcopy(pr)
        self.assertTrue(approved_patch(pr, proof))
        self.assertEqual(review_decision(pr), "REVIEW_REQUIRED")
        self.assertIn("awaiting-import", pr_states(pr, proof))
        self.assertNotIn("maintainer-review", pr_states(pr, proof))
        imported = {**original(), "number": 2, "status": "open", "matchesSourceHead": True}
        pr["imports"] = [imported]
        self.assertIn("importing", pr_states(pr, proof))
        self.assertNotIn("maintainer-review", pr_states(pr, proof))
        imported.update(matchesSourceHead=False, sourceHead=proof["approvedHead"])
        self.assertIn("importing", pr_states(pr, proof))
        self.assertNotIn("importing", pr_states(pr))
        imported["status"] = "merged"
        self.assertIn("imported", pr_states(pr, proof))
        self.assertNotIn("awaiting-import", pr_states(pr, proof))
        pr["imports"] = []
        self.assertEqual(pr, raw)
        later = {"kind": "review", "id": "new-approval", "state": "APPROVED", "body": "",
                 "submittedAt": "2026-10-19T20:00:00Z", "author": {"login": "reviewer"},
                 "commit": {"oid": pr["head"]}}
        pr["feedback"]["items"].append(later)
        self.assertTrue(approved_patch(pr, proof))
        later["body"] = "Approved, but wait for the dependency."
        self.assertFalse(approved_patch(pr, proof))
        pr["feedback"]["items"].pop()
        # Provider reassociation does not alter the retained review provenance.
        pr["feedback"]["items"][0]["commit"]["oid"] = pr["head"]
        self.assertTrue(approved_patch(pr, proof))
        pr["feedback"]["items"].append({"kind": "comment", "author": {"login": "tamird"},
                                          "createdAt": "2026-10-19T20:00:00Z", "body": "Please review the rebase."})
        self.assertTrue(approved_patch(pr, proof))
        for change in [{"head": "d" * 40}, {"repo": "buildbuddy-io/buildbuddy"},
                       {"githubReviewDecision": "CHANGES_REQUESTED"}, {"reviewsComplete": False},
                       {"threads": {"complete": True, "unresolved": 1}},
                       {"reviewRequests": [{"login": "reviewer"}]}]:
            with self.subTest(change=change):
                candidate = {**pr, **change}
                self.assertFalse(approved_patch(candidate, proof))
                self.assertNotIn("awaiting-import", pr_states(candidate, proof))
        for key, value in [("head", "d" * 40), ("reviewId", "different"), ("reviewer", "other"),
                           ("submittedAt", "2026-10-10T00:00:00Z"), ("newPatchId", "d" * 40),
                           ("verifiedAt", "not-a-time")]:
            with self.subTest(key=key):
                self.assertFalse(approved_patch(pr, {**proof, key: value}))
        for association in [None, "d" * 40]:
            candidate = copy.deepcopy(pr)
            candidate["feedback"]["items"][0]["commit"]["oid"] = association
            self.assertFalse(approved_patch(candidate, proof))
        edited = copy.deepcopy(pr)
        edited["feedback"]["items"][0]["updatedAt"] = "2026-10-19T20:00:00Z"
        self.assertFalse(approved_patch(edited, proof))
        for state in ["DISMISSED", "CHANGES_REQUESTED"]:
            candidate = copy.deepcopy(pr)
            candidate["feedback"]["items"][0]["state"] = state
            self.assertFalse(approved_patch(candidate, proof))
        pr["feedback"]["items"].append({"kind": "comment", "author": {"login": "reviewer"},
                                          "createdAt": "2026-10-19T20:00:00Z", "body": "Please hold this."})
        self.assertFalse(approved_patch(pr, proof))

    def test_incomplete_head_history_cannot_grant_approval(self):
        pr = original(reviewDecision="APPROVED", headHistoryComplete=False)
        self.assertEqual(review_decision(pr), "REVIEW_REQUIRED")
        del pr["headHistoryComplete"]
        self.assertEqual(review_decision(pr), "REVIEW_REQUIRED")

    def test_missing_head_time_is_unknown_not_approval(self):
        pr = original(reviewDecision="APPROVED")
        del pr["headCommittedAt"]
        self.assertEqual(review_decision(pr), "REVIEW_REQUIRED")
        pr["headCommittedAt"] = "not-a-date"
        self.assertEqual(review_decision(pr), "REVIEW_REQUIRED")

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
        self.assertNotIn("failing", record_states([pr], {"meta": {"repo": "google/gvisor"}, "nodes": []}, previous, "2026-10-02T00:00:00Z")["pr:1"])
        pr["checks"]["contexts"][1]["state"] = "FAILURE"
        self.assertEqual(pr_states(pr)["failing"]["qualifier"], "Source checks failing")

    def test_administrative_identity_requires_provider_and_workflow(self):
        check = {"kind": "check-run", "appSlug": "github-actions", "workflow": "Auto Assign", "name": "assign"}
        self.assertEqual(check_purpose(check), "reviewer-assignment")
        for change in ({"kind": "status"}, {"appSlug": "other"}, {"workflow": None},
                       {"workflow": "Build"}, {"name": "lint"}):
            with self.subTest(change=change):
                self.assertEqual(check_purpose({**check, **change}), "validation")

    def test_canceled_buildkite_is_neither_failed_validation_nor_success(self):
        canceled = {"kind": "status", "name": "buildkite/first-pass", "state": "FAILURE",
                    "description": "Build #37 canceled by Ryan El Kochta",
                    "url": "https://buildkite.com/gvisor/first-pass/builds/37"}
        pr = original(checks={"state": "FAILURE", "total": 1, "complete": True, "contexts": [canceled]})
        self.assertNotIn("failing", pr_states(pr))
        self.assertEqual(check_qualifier(pr), "Checks canceled")
        self.assertFalse(checks_passed(pr))
        imported = {**pr, "number": 2, "matchesSourceHead": True}
        source = original(imports=[imported])
        self.assertNotIn("failing", pr_states(source))
        self.assertIn("checks canceled", pr_states(source)["importing"]["qualifier"])
        for change in ({"description": "Build #37 failed"}, {"url": canceled["url"] + "0"}):
            with self.subTest(change=change):
                pr["checks"]["contexts"] = [{**canceled, **change}]
                self.assertIn("failing", pr_states(pr))
        pr["checks"]["contexts"] = [canceled]
        pr["checks"]["complete"] = False
        self.assertIn("failing", pr_states(pr))

    def test_closed_import_failure_stays_until_replaced(self):
        imported = {**original(), "number": 2, "matchesSourceHead": True, "status": "closed",
                    "checks": {"state": "FAILURE", "total": 1, "complete": True,
                               "contexts": [{"name": "test", "state": "FAILURE"}]}}
        source = original(imports=[imported])
        self.assertIn("failing", pr_states(source))
        replacement = {**original(), "number": 3, "matchesSourceHead": True}
        source["imports"].append(replacement)
        self.assertNotIn("failing", pr_states(source))
        replacement["status"] = "merged"
        self.assertNotIn("failing", pr_states(source))

    def test_import_lifecycle_is_separate_from_checks_and_review_reminders(self):
        pr = original(reviewDecision="APPROVED", mergeable="MERGEABLE")
        imported = original(number=2, head="b" * 40, matchesSourceHead=True, status="closed",
                            checks={"state": "FAILURE", "total": 1, "complete": True,
                                    "contexts": [{"name": "test", "state": "FAILURE"}]})
        pr["imports"] = [imported]
        disposition = public_import_disposition(pr)
        self.assertEqual((disposition["state"], disposition["action"]), ("closed", "maintainer-reimport"))
        self.assertIn("failing", pr_states(pr))
        pr["importsComplete"] = False
        self.assertEqual(public_import_disposition(pr)["state"], "unknown")
        self.assertIsNone(public_import_disposition(pr)["action"])
        pr["importsComplete"] = True
        pr["mergeable"] = "CONFLICTING"
        self.assertIsNone(public_import_disposition(pr)["action"])
        pr["mergeable"] = "MERGEABLE"
        imported["status"] = "open"
        self.assertEqual(public_import_disposition(pr)["state"], "open")
        imported["sourceHead"] = "c" * 40
        self.assertEqual(public_import_disposition(pr)["state"], "absent")

    def test_exact_landing_retires_import_action_without_rewriting_source(self):
        pr = original(reviewDecision="APPROVED")
        proof = {"head": pr["head"], "method": "merge-tree-identical", "upstreamHead": "b" * 40,
                 "upstreamTree": "c" * 40, "replayedTree": "c" * 40,
                 "verifiedAt": "2026-10-27T18:00:00Z", "evidence": ["https://github.com/google/gvisor/pull/16326"]}
        self.assertEqual(pr_states(pr, landing=proof),
                         {"imported": {"scope": pr["head"], "qualifier": "Landed; source PR still open"}})
        self.assertEqual(pr["status"], "open")
        for change in ({"head": "d" * 40}, {"replayedTree": "d" * 40}, {"verifiedAt": "unknown"}):
            with self.subTest(change=change):
                self.assertNotIn("imported", pr_states(pr, landing={**proof, **change}))

    def test_copybara_status_is_not_public_validation(self):
        lint = {"name": "lint", "state": "SUCCESS", "purpose": "validation"}
        for name in ("import/copybara", "feedback/copybara"):
            with self.subTest(name=name):
                service = {"kind": "status", "name": name, "state": "ERROR", "purpose": "validation"}
                self.assertEqual(check_purpose(service), "validation")
                self.assertEqual(check_purpose({**service, "kind": "check-run"}), "validation")
                pr = original(reviewDecision="APPROVED", mergeable="MERGEABLE",
                              checks={"state": "FAILURE", "total": 2, "complete": True,
                                      "contexts": [service, lint.copy()]})
                self.assertNotIn("failing", pr_states(pr))
                self.assertEqual(pr_states(pr)["awaiting-import"]["qualifier"],
                                 ("Internal import failed" if name == "import/copybara" else "Internal checks failed")
                                 + "; no public logs; public validation checks passed")
                self.assertFalse(checks_passed(pr))
                pr["checks"]["complete"] = False
                self.assertIn("failing", pr_states(pr))
                pr["checks"]["complete"] = True
                pr["checks"]["contexts"][1]["state"] = "FAILURE"
                self.assertEqual(pr_states(pr)["failing"]["qualifier"], "Source checks failing")

    def test_internal_import_and_feedback_keep_public_export_distinct(self):
        # Current #16097 and #15648 have an internal import but no public PR.
        lint = {"name": "lint", "state": "SUCCESS", "purpose": "validation"}
        imported = {"kind": "status", "name": "import/copybara", "state": "SUCCESS", "url": None}
        feedback = {"kind": "status", "name": "feedback/copybara", "state": "SUCCESS", "url": None}
        pr = original(reviewDecision="APPROVED", mergeable="MERGEABLE",
                      checks={"state": "SUCCESS", "total": 3, "complete": True,
                              "contexts": [lint, imported, feedback]})
        result = pr_states(pr)
        self.assertEqual(result["awaiting-import"]["scope"], pr["head"])
        self.assertEqual(result["awaiting-import"]["qualifier"],
                         "Internal import succeeded; Internal checks reported passing; public validation checks passed")
        self.assertNotIn("waiting-merge", result)
        feedback["state"] = "FAILURE"
        pr["checks"]["state"] = "FAILURE"
        result = pr_states(pr)
        self.assertEqual(result["awaiting-import"]["qualifier"],
                         "Internal checks failed; no public logs; Internal import succeeded; public validation checks passed")
        self.assertNotIn("failing", result)
        self.assertFalse(checks_passed(pr))
        lint["state"] = "FAILURE"
        self.assertIn("failing", pr_states(pr))
        self.assertIn("public checks failing", pr_states(pr)["awaiting-import"]["qualifier"])

    def test_public_import_failure_remains_actionable_with_copybara_error(self):
        imported = original(number=2, head="b" * 40, matchesSourceHead=True,
                            checks={"state": "FAILURE", "complete": True, "total": 1,
                                    "contexts": [{"name": "generate", "state": "FAILURE"}]})
        pr = original(reviewDecision="APPROVED", imports=[imported],
                      checks={"state": "FAILURE", "complete": True, "total": 2,
                              "contexts": [{"kind": "status", "name": "import/copybara", "state": "ERROR"},
                                           {"name": "lint", "state": "SUCCESS"}]})
        self.assertEqual(pr_states(pr)["failing"]["qualifier"], "Import #2 failing")
        self.assertNotIn("waiting-merge", pr_states(pr))

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

    def test_current_import_failure_survives_closure_until_landing(self):
        imported = original(number=2, head="b" * 40, matchesSourceHead=False,
                            checks={"state": "FAILURE", "contexts": [{"state": "FAILURE"}], "complete": True})
        pr = original(reviewDecision="APPROVED", imports=[imported])
        self.assertEqual(set(pr_states(pr)), {"awaiting-import"})
        imported["matchesSourceHead"] = True
        self.assertEqual(set(pr_states(pr)), {"failing", "importing"})
        self.assertEqual(pr_states(pr)["failing"]["qualifier"], "Import #2 failing")
        imported["status"] = "closed"
        self.assertEqual(set(pr_states(pr)), {"failing", "awaiting-import"})
        imported["status"] = "merged"
        self.assertEqual(set(pr_states(pr)), {"imported"})

    def test_failure_and_pending_overlap(self):
        pr = original(checks={"state": "PENDING", "contexts": [{"state": "FAILURE"}, {"state": "PENDING"}]})
        self.assertEqual(set(pr_states(pr)), {"maintainer-review", "failing", "checks-pending"})

    def test_same_head_age_survives_refresh_but_not_state_or_head_change(self):
        registry = {"meta": {"repo": "google/gvisor"}, "nodes": []}
        pr = original()
        first = {"checkedAt": "2026-10-01T00:00:00Z", "prs": [copy.deepcopy(pr)]}
        second = record_states([pr], registry, first, "2026-10-02T00:00:00Z")
        self.assertEqual(second["pr:1"]["maintainer-review"]["since"], first["checkedAt"])
        previous = {"checkedAt": "2026-10-02T00:00:00Z", "prs": [copy.deepcopy(pr)], "workStates": second}
        pr["updatedAt"] = "2026-10-03T00:00:00Z"
        refreshed = record_states([pr], registry, previous, "2026-10-03T00:00:00Z")
        self.assertEqual(refreshed["pr:1"]["maintainer-review"], {
            **second["pr:1"]["maintainer-review"], "observedAt": "2026-10-03T00:00:00Z"})
        pr["head"] = "c" * 40
        self.assertEqual(record_states([pr], registry, previous, "2026-10-03T00:00:00Z")["pr:1"]["maintainer-review"]["since"], "2026-10-03T00:00:00Z")
        previous = {"checkedAt": "2026-10-03T00:00:00Z", "prs": [original(reviewDecision="APPROVED")], "workStates": {"pr:1": {}}}
        self.assertEqual(record_states([original()], registry, previous, "2026-10-04T00:00:00Z")["pr:1"]["maintainer-review"]["since"], "2026-10-04T00:00:00Z")

    def test_exact_close_timestamp_and_curated_head(self):
        branch = {"id": "branch:other", "type": "branch", "repo": "tamird/gvisor", "ref": "other", "head": "b" * 40,
                  "status": "contributor review requested",
                  "contributorReview": {"status": "pending", "head": "b" * 40}}
        registry = {"meta": {"repo": "google/gvisor"}, "nodes": [branch]}
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

    def test_same_branch_name_in_another_repository_is_not_promoted(self):
        branch = {"id": "branch:go", "type": "branch", "repo": "tamird/go", "ref": "topic",
                  "head": "b" * 40, "contributorReview": {"status": "pending", "head": "b" * 40}}
        registry = {"meta": {"repo": "google/gvisor"}, "nodes": [branch]}
        states = record_states([original()], registry, {}, "2026-10-02T00:00:00Z")
        self.assertEqual(states[branch["id"]]["contributor-review"]["scope"], branch["head"])
        branch["repo"] = "tamird/gvisor"
        self.assertNotIn(branch["id"], record_states([original()], registry, {}, "2026-10-02T00:00:00Z"))
        for head_repo in (None, "tamird/renamed-fork"):
            with self.subTest(head_repo=head_repo):
                states = record_states([original(headRepo=head_repo)], registry, {}, "2026-10-02T00:00:00Z")
                self.assertIn(branch["id"], states)
        branch["repo"] = "tamird/renamed-fork"
        self.assertNotIn(branch["id"], record_states([original(headRepo="tamird/renamed-fork")], registry, {}, "2026-10-02T00:00:00Z"))

    def test_contributor_changes_are_current_head_branch_evidence(self):
        branch = {"id": "branch:revision", "type": "branch", "ref": "revision",
                  "head": "b" * 40,
                  "contributorChangesRequested": {"head": "b" * 40}}
        registry = {"meta": {"repo": "google/gvisor"}, "nodes": [branch]}
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
        current = record_states([], {"meta": {"repo": "google/gvisor"}, "nodes": [branch]}, previous, "2026-10-02T00:00:00Z")
        self.assertEqual(set(current[branch["id"]]), {"changes-requested"})
        self.assertEqual(current[branch["id"]]["changes-requested"]["since"], "2026-10-02T00:00:00Z")
        branch["ref"] = "topic"
        branch["repo"] = "tamird/gvisor"
        self.assertNotIn(branch["id"], record_states([original()], {"meta": {"repo": "google/gvisor"}, "nodes": [branch]}, previous,
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

    def test_legacy_amendment_requires_explicit_pr_kind_or_branch(self):
        node = {"id": "pr:1", "type": "pr", "status": "open",
                "contributorReview": {"status": "pending", "head": "c" * 40}}
        branch = {"id": "branch:amendment", "type": "branch", "ref": "review/amendment",
                  "contributorReview": copy.deepcopy(node["contributorReview"])}
        registry = {"meta": {"repo": "google/gvisor"}, "nodes": [node, branch]}
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

    def test_settled_amendment_is_not_a_new_contributor_decision(self):
        pr = original()
        candidate = "c" * 40
        node = {"id": "pr:1", "type": "pr",
                "contributorReview": {"kind": "amendment", "head": candidate,
                                      "baseHead": pr["head"], "status": "pending"},
                "settledReviews": [{"kind": "amendment", "head": candidate, "outcome": "approved"}]}
        registry = {"meta": {"repo": "google/gvisor"}, "nodes": [node]}
        now = "2026-10-02T00:00:00Z"
        self.assertNotIn("contributor-review", record_states([pr], registry, {}, now)["pr:1"])
        node["contributorReview"]["head"] = "d" * 40
        self.assertIn("contributor-review", record_states([pr], registry, {}, now)["pr:1"])

    def test_settling_either_inline_review_keeps_the_other_pending(self):
        pr = original()
        restore = {"kind": "review", "status": "pending", "head": pr["head"], "body": "Restore"}
        boot = {**restore, "body": "Boot"}
        node = {"id": "pr:1", "type": "pr", "contributorReview": restore, "additionalReviews": [boot]}
        registry = {"meta": {"repo": "google/gvisor", "updatedAt": "2026-10-30T00:00:00Z"}, "nodes": [node]}
        for settled in [[restore], [boot], [restore, boot]]:
            node["settledReviews"] = settled
            states = record_states([pr], registry, {}, registry["meta"]["updatedAt"])
            self.assertEqual("contributor-review" in states.get("pr:1", {}), len(settled) == 1)

    def test_outgoing_decisions_use_registry_clock_and_do_not_replace_public_review(self):
        pr = original(reviewDecision="CHANGES_REQUESTED")
        node = {"id": "pr:1", "type": "pr", "contributorReview": {
            "status": "pending", "kind": "review", "head": pr["head"]}}
        issue = {"id": "issue:2", "type": "issue", "contributorReview": {
            "status": "pending", "kind": "reply", "url": "https://github.com/google/gvisor/issues/2"}}
        registry = {"meta": {"repo": "google/gvisor", "updatedAt": "2026-10-03T00:00:00Z"},
                    "nodes": [node, issue]}
        states = record_states([pr], registry, {}, "2026-10-02T00:00:00Z")
        self.assertEqual(states["pr:1"]["contributor-review"]["scope"], pr["head"])
        self.assertEqual(states["pr:1"]["contributor-review"]["observedAt"], registry["meta"]["updatedAt"])
        self.assertIn("changes-requested", states["pr:1"])
        self.assertEqual(states["issue:2"]["contributor-review"]["scope"], issue["contributorReview"]["url"])
        pr["head"] = "d" * 40
        self.assertNotIn("contributor-review", record_states([pr], registry, {}, registry["meta"]["updatedAt"])["pr:1"])
        del node["contributorReview"]
        self.assertNotIn("contributor-review", record_states([pr], registry, {"workStates": states}, "2026-10-03T00:00:00Z")["pr:1"])

    def test_amendment_only_needs_review_against_its_published_base(self):
        pr = original(reviewDecision="CHANGES_REQUESTED")
        node = {"id": "pr:1", "type": "pr", "contributorReview": {
            "status": "pending", "kind": "amendment", "head": "c" * 40,
            "baseHead": pr["head"]}}
        registry = {"meta": {"repo": "google/gvisor", "updatedAt": "2026-10-03T00:00:00Z"},
                    "nodes": [node]}
        states = record_states([pr], registry, {}, "2026-10-02T00:00:00Z")
        decision = states["pr:1"]["contributor-review"]
        self.assertEqual(decision["scope"], "c" * 40)
        self.assertEqual(decision["observedAt"], registry["meta"]["updatedAt"])
        self.assertIn("changes-requested", states["pr:1"])
        pr["head"] = "d" * 40
        changed = record_states([pr], registry, {"workStates": states}, "2026-10-04T00:00:00Z")
        self.assertNotIn("contributor-review", changed["pr:1"])
        self.assertEqual(changed["pr:1"]["changes-requested"]["scope"], pr["head"])
        pr["head"] = node["contributorReview"]["head"]
        published = record_states([pr], registry, {"workStates": changed}, "2026-10-04T00:00:00Z")
        self.assertNotIn("contributor-review", published["pr:1"])
        del node["contributorReview"]
        self.assertNotIn("contributor-review", record_states([pr], registry, {"workStates": changed},
                                                             "2026-10-04T00:00:00Z")["pr:1"])

    def test_legacy_review_age_migration_keeps_authorities_separate(self):
        pr = original()
        branch = {"id": "branch:other", "type": "branch", "ref": "other",
                  "contributorReview": {"status": "pending", "head": "b" * 40}}
        old = {"scope": pr["head"], "since": "2026-09-01T00:00:00Z", "basis": "observed"}
        previous = {"checkedAt": "2026-10-01T00:00:00Z", "prs": [pr], "workStates": {
            "pr:1": {"review": old}, "branch:other": {"review": {**old, "scope": "b" * 40}}}}
        result = record_states([pr], {"meta": {"repo": "google/gvisor"}, "nodes": [branch]}, previous, "2026-10-02T00:00:00Z")
        self.assertEqual(result["pr:1"]["maintainer-review"]["since"], old["since"])
        self.assertEqual(result["branch:other"]["contributor-review"]["since"], old["since"])
        branch["contributorReview"]["head"] = "c" * 40
        result = record_states([pr], {"meta": {"repo": "google/gvisor"}, "nodes": [branch]}, previous, "2026-10-02T00:00:00Z")
        self.assertEqual(result["branch:other"]["contributor-review"]["since"], "2026-10-02T00:00:00Z")

    def test_new_head_rule_does_not_backdate_changed_review_state(self):
        pr = original(reviewDecision="APPROVED")
        pr["head"] = "c" * 40
        previous = {"checkedAt": "2026-10-01T00:00:00Z", "prs": [pr], "workStates": {"pr:1": {}}}
        recorded = record_states([pr], {"meta": {"repo": "google/gvisor"}, "nodes": []}, previous, "2026-10-02T00:00:00Z")
        self.assertEqual(recorded["pr:1"]["maintainer-review"]["since"], "2026-10-02T00:00:00Z")

    def test_prepared_proposal_is_not_a_review_request(self):
        branch = {"id": "branch:proposal", "type": "branch", "ref": "review/proposal",
                  "head": "b" * 40, "status": "Prepared proposal"}
        pr = original(reviewDecision="APPROVED")
        result = record_states([pr], {"meta": {"repo": "google/gvisor"}, "nodes": [branch]}, {}, "2026-10-02T00:00:00Z")
        self.assertNotIn("branch:proposal", result)
        self.assertEqual(set(result["pr:1"]), {"awaiting-import"})

if __name__ == "__main__":
    unittest.main()


class RepositoryStateTest(unittest.TestCase):
    def test_direct_approval_and_same_number_have_separate_states(self):
        primary = original(reviewDecision="APPROVED")
        external = original(reviewDecision="APPROVED", repo="buildbuddy-io/buildbuddy", importPolicy="none",
                            mergeable="MERGEABLE", checks={"complete": True, "total": 1, "state": "SUCCESS",
                            "contexts": [{"name": "tests", "state": "SUCCESS", "purpose": "validation"}]})
        registry = {"meta": {"repo": "google/gvisor"}, "nodes": []}
        states = record_states([primary, external], registry, {}, "2026-10-19T00:00:00Z")
        self.assertIn("awaiting-import", states["pr:1"])
        self.assertEqual(set(states["pr:buildbuddy-io/buildbuddy:1"]), {"waiting-merge"})
        external["checks"]["contexts"][0]["state"] = "FAILURE"
        self.assertNotIn("waiting-merge", pr_states(external))
        self.assertIn("failing", pr_states(external))


class ApprovalGateTest(unittest.TestCase):
    def test_exact_provider_gate_preserves_raw_error_and_blocks_readiness(self):
        gate = {"name": "Test (darwin_arm64)", "kind": "status", "state": "ERROR",
                "description": "Check requires approving review", "url": "https://app.buildbuddy.io"}
        pr = original(repo="buildbuddy-io/buildbuddy", importPolicy="none", reviewDecision="APPROVED",
                      mergeable="MERGEABLE", checks={"complete": True, "total": 1, "state": "FAILURE", "contexts": [gate]})
        self.assertEqual(set(pr_states(pr)), {"maintainer-review"})
        self.assertFalse(checks_passed(pr))
        self.assertEqual(gate["state"], "ERROR")
        mixed = copy.deepcopy(pr)
        mixed["checks"]["total"] = 2
        mixed["checks"]["contexts"].append({"name": "linux", "state": "SUCCESS"})
        self.assertFalse(checks_passed(mixed))
        self.assertEqual(set(pr_states(mixed)), {"maintainer-review"})
        for changed in ({"description": None}, {"description": "Check requires approving review later"},
                        {"url": "https://example.com"}, {"kind": "check-run"}, {"state": "FAILURE"}):
            with self.subTest(changed=changed):
                other = copy.deepcopy(pr)
                other["checks"]["contexts"][0].update(changed)
                self.assertIn("failing", pr_states(other))
        self.assertIn("failing", pr_states({**pr, "repo": "google/gvisor"}))
        pr["checks"]["complete"] = False
        self.assertIn("failing", pr_states(pr))
        pr["checks"].update(complete=True, total=2)
        pr["checks"]["contexts"].append({"name": "linux", "state": "FAILURE"})
        self.assertIn("failing", pr_states(pr))
        self.assertIn("maintainer-review", pr_states(pr))
