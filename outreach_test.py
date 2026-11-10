"""Exercise review deadlines and suppression with normalized GitHub evidence."""

import unittest
from datetime import datetime
from unittest.mock import patch

from outreach import business_deadline, plan, requested_in_comment
from reviewer_selection import Experience, History, reviewer_history

HEAD = "a" * 40
OLD = "2026-10-01T16:00:00Z"
START = "2026-10-26T16:00:00Z"  # 09:00 Pacific, a week before the DST change.
NOW = "2026-11-09T20:00:00Z"
OWNER = {"login": "tamird", "__typename": "User"}


def pr():
    return {"repo": "google/gvisor", "number": 1, "head": HEAD, "branchHead": HEAD, "status": "open",
            "mergeable": "MERGEABLE", "authorLogin": "tamird", "headOwner": "tamird",
            "reviewsComplete": True, "reviewRequestsComplete": True, "importsComplete": True,
            "reviewDismissals": {"complete": True, "items": []},
            "headHistoryComplete": True, "files": ["pkg/sentry/kernel/task.go"],
            "headCommittedAt": START, "createdAt": OLD, "feedback": {"complete": True, "items": []},
            "checks": {"complete": True}, "threads": {"complete": True, "unresolved": 0},
            "reviewRequests": [], "reviewTimeline": {"complete": True, "items": []}}


def action(p, when=NOW, **kwargs):
    return plan(p, kwargs.pop("node", {}), kwargs.pop("states", {}), when, "tamird", {"alice","bob"}, {}, **kwargs)


def ask(who, at, bot=False, kind="request"):
    return {"kind": kind, "actor": {"login": "github-actions" if bot else "tamird",
            "__typename": "Bot" if bot else "User"}, "reviewer": {"login": who}, "at": at}


def comment(who, text, at, **extra):
    return {"id": at, "kind": "comment", "author": {"login": who, "__typename": "User"},
            "body": text, "createdAt": at, "updatedAt": at, **extra}


class OutreachTest(unittest.TestCase):
    def setUp(self):
        self.comp = patch("outreach.get_reviewer_candidates", return_value=[
            {"login": r, "reason": "You reviewed this file.", "scope": "pkg/sentry/kernel/task.go", "channel": "comment",
             "evidence": "https://github.com/google/gvisor/pull/1"} for r in ("alice", "bob")])
        self.comp.start()
        self.addCleanup(self.comp.stop)

    def approved(self):
        p = pr()
        p.update(url="https://github.com/google/gvisor/pull/1", importPolicy="copybara",
                 githubReviewDecision="APPROVED", maintainerReviewers=["alice"],
                 publicImportDisposition={"scope": "none", "state": "absent", "trigger": {"state": "present"}})
        p["feedback"]["items"].append(comment("alice", "LGTM", START, kind="review", state="APPROVED",
            submittedAt=START, commit={"oid": HEAD}, authorAssociation="MEMBER", url=p["url"]+"#pullrequestreview-1"))
        p["checks"]["contexts"] = [{"name": "import/copybara", "state": "SUCCESS", "internalCL": "1010626048"}]
        return p

    def import_plan(self, p, now=NOW, **kwargs):
        history = History({who: Experience() for who in ("alice", "bob")}, {}, "tamird",
                          {"bob": "https://github.com/google/gvisor/pull/99"})
        return plan(p, kwargs.pop("node", {}), kwargs.pop("states", {}), now, "tamird", set(history.people),
                    history, **kwargs)

    def test_import_without_export_and_with_failed_internal_feedback(self):
        p = self.approved()
        a = self.import_plan(p)
        self.assertEqual((a["kind"], a["reviewer"], a["channel"]), ("import-status", "alice", "comment"))
        self.assertIn("CL 1010626048", a["body"])
        self.assertIn("no public export", a["body"])
        p["checks"]["contexts"].append({"name": "feedback/copybara", "state": "FAILURE"})
        self.assertIn("`feedback/copybara` failed", self.import_plan(p)["body"])
        self.assertNotIn("checks passed", self.import_plan(p)["body"])

    def test_manual_import_status_contact_and_bot_export_on_16586(self):
        p = self.approved()
        p.update(number=16586, url="https://github.com/google/gvisor/pull/16586")
        export = {"number":16983, "status":"open", "sourceHead":HEAD, "feedback":{"complete":True, "items":[]}}
        p["imports"] = [export]
        receipt = p["url"] + "#issuecomment-6774296987"
        p["feedback"]["items"].append(comment("tamird", "@alice, could you check what CL 1010626048 needs to land? "
             "Copybara imported the change after your October 30 approval, the internal checks passed, and the "
             "export (#16983) is still open.", "2026-11-10T17:26:13Z", url=receipt))
        export["feedback"]["items"].append(comment("copybara-service", "Refreshed", "2026-11-11T18:00:00Z",
            url="https://github.com/google/gvisor/pull/16983"))
        row = self.import_plan(p, "2026-11-12T17:26:12Z")
        self.assertEqual((row["status"],row["lastContactURL"],row["dueAt"]),
                         ("waiting",receipt,"2026-11-12T09:26:13-08:00"))
        self.assertEqual(self.import_plan(p,"2026-11-12T17:26:13Z")["reviewer"],"bob")
        response = "https://github.com/google/gvisor/pull/16983#issuecomment-123"
        export["feedback"]["items"].append(comment("alice","Investigating the integration.", "2026-11-12T17:00:00Z",url=response))
        row = self.import_plan(p, "2026-11-12T18:00:00Z")
        self.assertEqual((row["lastActivityURL"],row["dueAt"]), (response,"2026-11-19T09:00:00-08:00"))

    def test_import_skips_prior_approval_hold_and_landed_export(self):
        p = self.approved()
        for action in ("maintainer-reapproval","maintainer-import-label"):
            p["publicImportDisposition"]["action"] = action
            self.assertIsNone(self.import_plan(p))
        del p["publicImportDisposition"]["action"]
        self.assertIsNone(self.import_plan(p, states={"prerequisite":{}}))
        self.assertIsNone(self.import_plan(p, node={"releaseWait":{"head":HEAD}}))
        p["imports"] = [{"number":2,"sourceHead":HEAD,"status":"merged"}]
        self.assertIsNone(self.import_plan(p))
        p["imports"] = []
        p["feedback"]["items"][0]["commit"]["oid"] = "b"*40
        self.assertIsNone(self.import_plan(p))

    def test_only_landed_public_imports_identify_alternates(self):
        p = self.approved()
        p["labelHistory"] = {"complete":True, "items":[{"__typename":"LabeledEvent",
            "actor":{"login":"bob","__typename":"User"},"label":{"name":"ready to pull"}}]}
        args = {"maintainers":{"alice","bob"}}
        self.assertEqual(reviewer_history([p],"tamird",**args).importers,{})
        p["imports"] = [{"status":"merged"}]
        self.assertEqual(reviewer_history([p],"tamird",**args).importers,{"bob":p["url"]})

    def test_waits_five_pacific_business_days_for_the_current_source(self):
        self.assertEqual(business_deadline(START,5).isoformat(), "2026-11-02T09:00:00-08:00")
        self.assertEqual(action(pr(), "2026-11-02T16:59:59Z")["status"], "waiting")
        self.assertEqual(action(pr(), "2026-11-02T17:00:00Z")["status"], "due")
        self.assertEqual(action(pr(), "2026-11-07T19:00:00Z")["status"], "waiting")
        p=pr()
        p["reviewTimeline"]["items"].append({"kind":"ready","at":"2026-11-06T20:00:00Z"})
        self.assertEqual(action(p)["dueAt"],"2026-11-13T12:00:00-08:00")

    def test_only_certified_unchanged_rebase_inherits_older_review_clock(self):
        p = pr()
        p.update(headIntroducedAt="2026-11-09T18:00:00Z", headRebasedFrom="b" * 40)
        proof = {"head": HEAD, "priorHead": "b" * 40, "method": "reviewed-context-only-rebase",
                 "changedLinesSha256": "1" * 64, "reviewableSince": START,
                 "evidence": "https://github.com/google/gvisor/runs/123"}
        self.assertEqual(action(p)["status"], "waiting")
        self.assertEqual(action(p, node={"outreachReviewClock": proof})["status"], "due")
        p["headRebasedFrom"] = "c" * 40
        self.assertEqual(action(p, node={"outreachReviewClock": proof})["status"], "waiting")

    def test_recent_personal_contact_and_rotation(self):
        for channel in ("request", "comment"):
            with self.subTest(channel=channel):
                p=pr()
                at="2026-11-06T18:00:00Z"
                if channel=="request":
                    p["reviewTimeline"]["items"]=[ask("alice",at)]
                    p["reviewRequests"]=[{"login":"alice"}]
                else:
                    p["feedback"]["items"]=[comment("tamird","@alice, could you take a look?",at)]
                self.assertEqual(action(p)["dueAt"],"2026-11-10T10:00:00-08:00")
                self.assertEqual(action(p)["lastReviewers"],["alice"])
                self.assertEqual(action(p,"2026-11-10T18:00:00Z")["reviewer"],"bob")

    def test_unchanged_bot_assignment_allows_one_effective_mention(self):
        p=pr()
        p["reviewTimeline"]["items"]=[ask("alice",START,bot=True)]
        p["reviewRequests"]=[{"login":"alice"}]
        first=action(p)
        self.assertEqual((first["reviewer"],first["channel"]),("alice","comment"))
        p["feedback"]["items"]=[comment("tamird",first["body"],NOW)]
        self.assertEqual(action(p)["status"],"waiting")
        self.assertEqual(action(p,"2026-11-12T20:00:00Z")["reviewer"],"bob")

    def test_personal_request_after_recent_review_has_its_own_two_days(self):
        p = pr()
        p["feedback"]["items"] = [
            comment("alice", "The new test covers the failure.", "2026-11-06T19:24:22Z", kind="review", state="COMMENTED"),
            comment("tamird", "@alice, could you approve this revision?", "2026-11-07T03:02:36Z"),
        ]
        self.assertEqual(action(p, "2026-11-11T03:02:35Z")["status"], "waiting")
        self.assertEqual(action(p, "2026-11-11T03:02:36Z")["reviewer"], "bob")

    def test_response_after_request_starts_five_idle_days(self):
        p = pr()
        p["feedback"]["items"] = [
            comment("tamird", "@alice, could you approve this revision?", "2026-11-07T03:02:36Z"),
            comment("alice", "The new test covers the failure.", "2026-11-09T20:00:00Z", kind="review", state="COMMENTED"),
        ]
        self.assertEqual(action(p, "2026-11-11T03:02:36Z")["dueAt"], "2026-11-16T12:00:00-08:00")

    def test_personal_request_survives_a_certified_equivalent_squash(self):
        p = pr()
        p.update(headIntroducedAt="2026-11-09T18:00:00Z", headRebasedFrom="b" * 40)
        p["feedback"]["items"] = [comment("tamird", "@alice, could you review?", "2026-11-07T03:02:36Z")]
        proof = {"head": HEAD, "priorHead": "b" * 40, "method": "reviewed-context-only-rebase",
                 "changedLinesSha256": "1" * 64, "reviewableSince": START,
                 "evidence": "https://github.com/google/gvisor/runs/123"}
        when = "2026-11-11T03:02:36Z"
        self.assertEqual(action(p, when)["status"], "waiting")
        self.assertEqual(action(p, when, node={"outreachReviewClock": proof})["reviewer"], "bob")

    def test_initial_author_attributed_codeowner_request_allows_personal_mention(self):
        p = pr()
        p.update(createdAt=START, codeOwnerRequests=["alice"], reviewRequests=[{"login": "alice"}])
        timeline = p["reviewTimeline"]["items"]
        timeline.append(ask("alice", "2026-10-26T16:00:03Z"))
        self.assertEqual(action(p)["reviewer"], "alice")
        timeline.append(ask("alice", "2026-11-06T18:00:00Z"))
        self.assertEqual(action(p)["dueAt"], "2026-11-10T10:00:00-08:00")
        self.assertEqual(action(p, "2026-11-10T18:00:00Z")["reviewer"], "bob")

    def test_reviewer_work_resumes_after_author_answer(self):
        p=pr();p["githubReviewDecision"]="CHANGES_REQUESTED"
        p["threads"]["unresolved"]=1
        p["feedback"]["items"]=[
            comment("alice","Please move this call.",START,kind="review",state="CHANGES_REQUESTED",commit={"oid":HEAD}),
            comment("alice","This breaks restore.",START,kind="inline",threadId="t",isResolved=False)]
        self.assertIsNone(action(p))
        p["feedback"]["items"].append(comment("tamird","Moved the call before save.","2026-10-27T16:00:00Z",
                                             kind="inline",threadId="t",isResolved=False))
        self.assertEqual(action(p)["status"],"due")
        self.assertIsNone(action(p,node={"reviewOutreachHold":{"evidence":"documented hold"}}))

    def test_incomplete_history_and_real_blockers(self):
        p=pr();p["reviewRequests"]=[{"login":"bob"}]
        self.assertEqual(action(p)["status"],"incomplete")
        for state in ("conflicts","failing","prerequisite","contributor-review","importing","awaiting-import"):
            with self.subTest(state=state): self.assertIsNone(action(pr(),states={state:{}}))
        p=pr();p["status"]="draft"
        self.assertIsNone(action(p))

    def test_unknown_mergeability_preserves_contacts_and_requires_a_live_verdict(self):
        p = pr()
        p.update(mergeable="UNKNOWN", base="b" * 40)
        pending = action(p)
        self.assertEqual((pending["status"], pending["base"]), ("verify-mergeability", "b" * 40))
        p["feedback"]["items"] = [comment("tamird", pending["body"], NOW)]
        self.assertEqual(action(p)["status"], "waiting")
        p["mergeable"] = "CONFLICTING"
        self.assertIsNone(action(p))

    def test_dismissed_approval_stays_with_prior_approver(self):
        p = pr()
        old_review = comment("alice", "", OLD, kind="review", state="DISMISSED", submittedAt=OLD,
                             id="old-approval", authorAssociation="MEMBER", commit={"oid": "b" * 40})
        p["feedback"]["items"].append(old_review)
        p["reviewDismissals"]["items"].append({"previousReviewState": "APPROVED", "review": {"id": "old-approval"}})
        self.assertIsNone(action(p))

    def test_machine_review_under_human_name_does_not_reset_idle_time(self):
        p = pr()
        p["feedback"]["items"] = [comment("alice", "[🤖🐸🤖] Review agent has no findings.", NOW,
                                          kind="review", state="COMMENTED")]
        self.assertEqual(action(p)["status"], "due")

    def test_quoted_request_and_delegation_footer_are_not_contacts(self):
        p=comment("tamird","> @alice please review\n\nPosted by Codex on behalf of @tamird.",NOW)
        self.assertFalse(requested_in_comment(p,1))
        p["body"]="@alice, could you review the checkpoint change?\n\nPosted by Codex on behalf of @tamird."
        self.assertEqual(requested_in_comment(p,1),{"alice"})

