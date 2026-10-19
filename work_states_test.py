"""Regressions for review/import classification and observation continuity."""

import copy
from importlib import import_module
import subprocess
import unittest
from unittest.mock import patch

from work_states import check_purpose, checks_passed, pr_states, record_states, remember_mergeability, review_decision


def original(**changes: object) -> dict:
    pr = {"number": 1, "status": "open", "head": "a" * 40, "ref": "topic", "headOwner": "tamird", "headRepo": "tamird/gvisor",
          "reviewDecision": None, "headHistoryComplete": True, "headCommittedAt": "2026-08-31T00:00:00Z",
          "imports": [], "importsComplete": True,
          "labels": [], "labelsComplete": True, "checks": None}
    if changes.get("reviewDecision") == "APPROVED":
        pr.update(reviewsComplete=True, reviewRequestsComplete=True, reviewRequests=[],
                  feedback={"items": [{"kind": "review", "state": "APPROVED", "submittedAt": "2026-09-01T00:00:00Z",
                                       "author": {"login": "reviewer"}, "commit": {"oid": changes.get("head", pr["head"])}}]})
    pr.update(changes)
    return pr


class WorkStatesTest(unittest.TestCase):
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
                                 "Import status failing; validation checks passed")
                self.assertFalse(checks_passed(pr))
                pr["checks"]["complete"] = False
                self.assertIn("failing", pr_states(pr))
                pr["checks"]["complete"] = True
                pr["checks"]["contexts"][1]["state"] = "FAILURE"
                self.assertEqual(pr_states(pr)["failing"]["qualifier"], "Source checks failing")

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

    def test_contributor_amendment_belongs_to_its_own_branch(self):
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
