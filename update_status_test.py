"""Collector history retention and reopening through the real main path."""

import copy
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock


updater = importlib.import_module("update-status")
OLD = "2000-01-01T00:00:00+00:00"
LATER = "2000-01-02T00:00:00+00:00"


def raw_pr(number: int, state: str = "OPEN", *, author: str = "tamird", head: int | None = None) -> dict:
    oid = f"{head if head is not None else number:040x}"
    return {
        "number": number, "title": f"PR {number}", "url": f"https://github.com/google/gvisor/pull/{number}",
        "state": state, "isDraft": False, "headRefName": f"topic-{number}", "headRefOid": oid,
        "baseRefOid": "f" * 40, "updatedAt": OLD, "createdAt": OLD,
        "mergedAt": OLD if state == "MERGED" else None,
        "mergeCommit": {"oid": "e" * 40} if state == "MERGED" else None,
        "closedAt": OLD if state != "OPEN" else None,
        "headRepository": {"nameWithOwner": "tamird/gvisor", "owner": {"login": "tamird"}},
        "repository": {"nameWithOwner": "google/gvisor"}, "author": {"login": author},
        "reviewDecision": None, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "commits": {"nodes": [{"commit": {"oid": oid, "committedDate": OLD, "statusCheckRollup": None}}]},
        "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "comments": {"nodes": [], "pageInfo": {"hasPreviousPage": False}},
        "reviews": {"nodes": [], "pageInfo": {"hasPreviousPage": False}},
        "reviewDismissals": {"nodes": [], "pageInfo": {"hasPreviousPage": False}},
        "reviewRequests": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "reviewThreads": {"nodes": [], "totalCount": 0, "pageInfo": {"hasNextPage": False}},
        "timelineItems": {"nodes": [], "pageInfo": {"hasPreviousPage": False}},
        "headHistory": {"nodes": []}, "_retrievedAt": OLD,
    }


def imported(number: int, source: int, state: str = "OPEN", *, head: int | None = None) -> dict:
    pr = raw_pr(number, state, author="copybara-service", head=head)
    pr["body"] = (f"FUTURE_COPYBARA_INTEGRATE_REVIEW=https://github.com/google/gvisor/pull/{source} "
                  f"from tamird:topic-{source} {source:040x}")
    return pr


def retained(pr: dict, imports: list[dict] | None = None) -> dict:
    item = updater.normalize(pr)
    item.update(checkedAt=OLD, importPolicy="copybara", importsComplete=True, imports=imports or [])
    return item


def linked(pr: dict, source: int) -> dict:
    item = retained(pr)
    item.update(sourceHead=f"{source:040x}", matchesSourceHead=True, relationshipEvidence=item["url"])
    return item


class CollectorHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "github-status.json"
        self.registry = {"meta": {"repo": "google/gvisor", "owner": "tamird", "updatedAt": OLD},
                         "nodes": [{"id": "pr:5", "type": "pr", "repo": "google/gvisor", "number": 5}]}
        self.previous = {
            "schema": 1, "repo": "google/gvisor", "owner": "tamird", "checkedAt": OLD,
            "prs": [retained(raw_pr(1, "MERGED"), [linked(imported(101, 1), 1), linked(imported(102, 1, "CLOSED"), 1)]),
                    retained(raw_pr(2, "CLOSED", author="other")),
                    retained(raw_pr(3), [linked(imported(103, 3, "CLOSED"), 3), linked(imported(104, 3, "CLOSED"), 3)]),
                    retained(raw_pr(4))],
        }
        # Retained terminal history can grow without adding detail queries.
        self.previous["prs"].extend(retained(raw_pr(n, "MERGED")) for n in range(200, 300))
        self.original_bytes = (json.dumps(self.previous) + "\n").encode()
        self.path.write_bytes(self.original_bytes)
        self.raw = {2: raw_pr(2, author="other", head=202), 3: raw_pr(3), 4: raw_pr(4, "MERGED"),
                    5: raw_pr(5, "CLOSED"), 6: raw_pr(6), 101: imported(101, 1),
                    103: imported(103, 3, head=113), 104: imported(104, 3, "CLOSED"), 105: imported(105, 3)}
        self.raw[4].update(mergedAt=LATER, closedAt=LATER, updatedAt=LATER)
        self.raw[3]["timelineItems"]["nodes"] = [{"source": self.raw[n]} for n in (103, 104, 105)]
        self.open_rows = [{"number": n, "author": {"login": self.raw[n]["author"]["login"]}}
                          for n in (2, 3, 6, 101, 103, 105)]
        self.open_rows += [{"number": n, "author": {"login": "untracked"}} for n in range(500, 600)]
        self.requested: list[int] = []
        self.inventory_pages = 0
        self.failure: str | None = None

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        updater.requests += 1
        if updater.requests > updater.REQUEST_LIMIT:
            raise RuntimeError("request cap exceeded")
        if "pullRequests(states:OPEN" in query:
            self.inventory_pages += 1
            offset = int((variables or {}).get("after") or 0)
            if self.failure == "inventory" and offset:
                raise updater.GitHubTransportError("inventory transport failed")
            rows = copy.deepcopy(self.open_rows[offset:offset + 100])
            return {"repository": {"pullRequests": {
                "totalCount": len(self.open_rows), "nodes": rows,
                "pageInfo": {"hasNextPage": offset + 100 < len(self.open_rows), "endCursor": str(offset + 100)},
            }}}
        numbers = [int(n) for n in re.findall(r"p(\d+):pullRequest", query)]
        self.assertTrue(numbers, query)
        self.requested.extend(numbers)
        if self.failure == "required":
            raise updater.GitHubTransportError("required detail transport failed")
        response = {f"p{n}": copy.deepcopy(self.raw[n]) for n in numbers}
        if self.failure == "footer" and "p105" in response:
            response["p105"]["body"] = response["p105"]["body"].replace(f"{3:040x}", "a" * 40)
        return {"repository": response}

    def collect(self) -> dict:
        updater.requests = 0
        with mock.patch.multiple(updater, ROOT=self.root, REGISTRY=self.registry), \
                mock.patch.object(updater, "graphql", side_effect=self.graphql), \
                mock.patch.object(updater.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            updater.main()
        return json.loads(self.path.read_text())

    def test_retention_reopening_closure_and_active_imports(self) -> None:
        current = self.collect()
        self.assertEqual(next(pr for pr in current["prs"] if pr["number"] == 4)["mergeCommit"], "e" * 40)
        self.assertEqual(self.inventory_pages, 2)
        self.assertEqual(set(self.requested), {2, 3, 4, 5, 6, 101, 103, 105})
        self.assertEqual(len(self.requested), 8)
        self.assertEqual(updater.REQUEST_LIMIT, 48)
        self.assertLess(updater.requests, 10)
        prs = {pr["number"]: pr for pr in current["prs"]}
        self.assertEqual(set(prs), {1, 2, 3, 4, 5, 6, *range(200, 300)})
        self.assertEqual(prs[1]["checkedAt"], OLD)
        self.assertEqual(prs[200], self.previous["prs"][4])
        checked_at = prs[2]["checkedAt"]
        self.assertGreater(datetime.fromisoformat(checked_at), datetime.fromisoformat(OLD))
        self.assertLessEqual(datetime.fromisoformat(checked_at), datetime.fromisoformat(current["checkedAt"]))
        self.assertEqual((prs[2]["status"], prs[2]["head"]), ("open", f"{202:040x}"))
        self.assertEqual(prs[4]["status"], "merged")
        self.assertEqual(prs[4]["checkedAt"], checked_at)
        imports = {item["number"]: item for pr in prs.values() for item in pr["imports"]}
        self.assertEqual(imports[101]["checkedAt"], checked_at)
        self.assertEqual(imports[102], self.previous["prs"][0]["imports"][1])
        self.assertEqual((imports[103]["status"], imports[103]["head"]), ("open", f"{113:040x}"))
        self.assertEqual(imports[103]["checkedAt"], checked_at)
        self.assertEqual(imports[104]["checkedAt"], OLD)
        self.assertTrue(imports[105]["matchesSourceHead"])
        self.assertEqual(current["workStates"]["pr:1"]["merged"]["observedAt"], OLD)
        self.assertEqual(current["limits"]["retainedSources"], 101)
        self.assertEqual(current["limits"]["retainedImports"], 2)

    def test_required_failure_or_footer_race_preserves_snapshot(self) -> None:
        for failure in ("inventory", "required", "footer"):
            with self.subTest(failure=failure):
                self.failure = failure
                with self.assertRaises(RuntimeError):
                    self.collect()
                self.assertEqual(self.path.read_bytes(), self.original_bytes)
                self.assertFalse((self.root / ".github-status.json.tmp").exists())

    def test_same_number_keeps_repository_history_and_direct_review_policy(self) -> None:
        repo = "buildbuddy-io/buildbuddy"
        self.registry["meta"]["repositories"] = {"google/gvisor": "copybara", repo: "none"}
        self.registry["nodes"].append({"id": f"pr:{repo}:5", "type": "pr", "repo": repo, "number": 5})
        raw = raw_pr(5)
        raw.update(repository={"nameWithOwner": repo}, url=f"https://github.com/{repo}/pull/5",
                   headRepository={"nameWithOwner": "tamird/buildbuddy", "owner": {"login": "tamird"}})
        primary_graphql = self.graphql

        def graphql(query: str, variables: dict | None = None) -> dict:
            if (variables or {}).get("owner") == "buildbuddy-io":
                return {"repository": {"pullRequests": {"totalCount": 1,
                        "nodes": [{"number": 5, "author": {"login": "tamird"}}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
            if 'owner:"buildbuddy-io"' in query:
                self.assertIn("CROSS_REFERENCED_EVENT", query)
                return {"repository": {"p5": copy.deepcopy(raw)}}
            return primary_graphql(query, variables)

        with mock.patch.object(self, "graphql", side_effect=graphql):
            current = self.collect()
        records = {(pr["repo"], pr["number"]): pr for pr in current["prs"]}
        self.assertEqual(records[("google/gvisor", 5)]["status"], "closed")
        self.assertEqual((records[(repo, 5)]["status"], records[(repo, 5)]["importPolicy"]), ("open", "none"))
        self.assertEqual(records[(repo, 5)]["imports"], [])
        self.assertIn("closed", current["workStates"]["pr:5"])
        self.assertIn("maintainer-review", current["workStates"][f"pr:{repo}:5"])
        self.assertEqual(current["repositories"], self.registry["meta"]["repositories"])

    def test_cross_references_surface_without_creating_relationships(self) -> None:
        repo = "buildbuddy-io/buildbuddy"
        self.registry["meta"]["repositories"] = {"google/gvisor": "copybara", repo: "none"}
        self.registry["nodes"].append({"id": f"pr:{repo}:13803", "type": "pr", "repo": repo, "number": 13803})
        raw = raw_pr(13803)
        raw.update(repository={"nameWithOwner": repo}, url=f"https://github.com/{repo}/pull/13803")
        replacement = {"id": "reference-13826", "createdAt": LATER, "source": {
            "__typename": "PullRequest", "number": 13826, "url": f"https://github.com/{repo}/pull/13826",
            "title": "Enable vhost-net", "body": "This supersedes #13803.", "updatedAt": LATER,
            "headRefOid": "a" * 40, "author": {"login": "bduffany"}, "repository": {"nameWithOwner": repo, "isPrivate": False},
        }}
        mention = copy.deepcopy(replacement)
        mention.update(id="unrelated-reference")
        mention["source"].update(number=99, url="https://github.com/other/repo/pull/99", body="Related discussion #13803.",
                                 repository={"nameWithOwner": "other/repo", "isPrivate": False}, author={"login": "other"})
        private = copy.deepcopy(mention)
        private.update(id="private-reference")
        private["source"].update(repository={"nameWithOwner": "other/private", "isPrivate": True}, body="Private description")
        raw["timelineItems"]["nodes"] = [replacement, mention, private,
                                        {"id": "unavailable-reference", "source": None},
                                        {"id": "issue-reference", "source": {"__typename": "Issue"}}]
        original_registry = copy.deepcopy(self.registry)
        primary_graphql = self.graphql

        def graphql(query: str, variables: dict | None = None) -> dict:
            if (variables or {}).get("owner") == "buildbuddy-io":
                return {"repository": {"pullRequests": {"totalCount": 2,
                        "nodes": [{"number": 13803, "author": {"login": "tamird"}},
                                  {"number": 13826, "author": {"login": "bduffany"}}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
            if 'owner:"buildbuddy-io"' in query:
                self.assertIn("CROSS_REFERENCED_EVENT", query)
                self.assertNotIn("p13826:", query)
                return {"repository": {"p13803": copy.deepcopy(raw)}}
            return primary_graphql(query, variables)

        with mock.patch.object(self, "graphql", side_effect=graphql):
            for expected in ([replacement, mention], [], [replacement]):
                with mock.patch("builtins.print") as output:
                    current = self.collect()
                pr = next(pr for pr in current["prs"] if pr["repo"] == repo and pr["number"] == 13803)
                self.assertEqual(pr["crossReferences"], {"complete": True, "items": [replacement, mention]})
                self.assertEqual(pr["imports"], [])
                self.assertNotIn(13826, [pr["number"] for pr in current["prs"]])
                events = [json.loads(call.args[0]) for call in output.call_args_list
                          if call.args[0].startswith('{') and '"crossReferences"' in call.args[0]]
                self.assertEqual([event["crossReferences"] for event in events if event["number"] == 13803],
                                 [expected] if expected else [])
                self.assertNotIn("Private description", self.path.read_text())
                self.assertNotIn("Private description", str(output.call_args_list))
                if not expected:
                    replacement["source"]["body"] = "Updated supersession explanation."
            raw["timelineItems"]["pageInfo"]["hasPreviousPage"] = True
            with mock.patch("builtins.print") as output:
                current = self.collect()
            self.assertTrue(any('"crossReferencesComplete": false' in call.args[0] for call in output.call_args_list))
        self.assertEqual(self.registry, original_registry)


class CheckReportTest(unittest.TestCase):
    def test_current_failure_old_import_and_resolution_remain_distinct(self) -> None:
        source = retained(raw_pr(3))
        old_import = linked(imported(103, 3), 3)
        failure = {"id": "status-1", "kind": "status", "name": "buildkite/pipeline", "state": "FAILURE"}
        old_import["checks"] = {"state": "FAILURE", "total": 1, "complete": True, "contexts": [failure]}
        source["imports"] = [old_import]
        previous = {"prs": [copy.deepcopy(source)]}
        source["head"] = "b" * 40
        old_import["matchesSourceHead"] = False
        report = updater.check_report([source], previous)
        self.assertEqual([(item["number"], item["scope"], item["sourceHead"], item["importedSourceHead"])
                          for item in report["checkFailures"]], [(103, "older-source-import", "b" * 40, f"{3:040x}")])
        self.assertEqual(report["activeImports"][0]["head"], old_import["head"])
        # An unchanged failed import remains visible in every sweep.
        self.assertEqual(report["changedChecks"], [])
        old_import["checks"] = {"state": "SUCCESS", "total": 1, "complete": True,
                                  "contexts": [{**failure, "state": "SUCCESS"}]}
        report = updater.check_report([source], previous)
        self.assertEqual(report["checkFailures"], [])
        self.assertEqual(report["changedChecks"][0]["checksState"], "SUCCESS")

    def test_internal_status_and_incomplete_rollup_are_reported(self) -> None:
        source = retained(raw_pr(3))
        failure = {"id": "copybara-1", "kind": "status", "name": "feedback/copybara", "state": "FAILURE"}
        source["checks"] = {"state": "FAILURE", "total": 1, "complete": True, "contexts": [failure]}
        report = updater.check_report([source], {})
        self.assertEqual(report["checkFailures"][0]["failedChecks"], [failure])
        previous = {"prs": [copy.deepcopy(source)]}
        source["head"] = "b" * 40
        self.assertEqual(len(updater.check_report([source], previous)["changedChecks"]), 1)
        source["checks"] = {"state": "FAILURE", "total": 101, "complete": False, "contexts": []}
        report = updater.check_report([source], previous)
        self.assertEqual((report["checkFailures"][0]["checksState"], report["checkFailures"][0]["checksComplete"]),
                         ("FAILURE", False))
        previous = {"prs": [copy.deepcopy(source)]}
        source["status"] = "closed"
        report = updater.check_report([source], previous)
        self.assertEqual(report["checkFailures"], [])
        self.assertEqual(report["changedChecks"][0]["status"], "closed")


class ScopedApprovalTest(unittest.TestCase):
    def test_scoped_detail_retains_approval_and_force_push_boundary(self) -> None:
        pr = raw_pr(7)
        pr["reviewDecision"] = "APPROVED"
        pr["headHistory"]["nodes"] = [{"createdAt": LATER, "afterCommit": {"oid": pr["headRefOid"]}}]
        review = {"id": "review-7", "url": pr["url"] + "#pullrequestreview-7", "body": "",
                  "state": "APPROVED", "submittedAt": LATER, "updatedAt": LATER,
                  "author": {"login": "reviewer", "__typename": "User"},
                  "commit": {"oid": pr["headRefOid"]}}
        pr["reviews"]["nodes"] = [review]

        def graphql(query: str) -> dict:
            response = copy.deepcopy(pr)
            # Return only requested history, as the service does. A detail-only
            # query must still retrieve the evidence used to judge approval.
            if "headHistory:timelineItems" not in query:
                response.pop("headHistory")
            return {"repository": {"p7": response}}

        with mock.patch.object(updater, "graphql", side_effect=graphql):
            for submitted, expected in [(LATER, "APPROVED"), (OLD, "REVIEW_REQUIRED")]:
                with self.subTest(submitted=submitted):
                    review["submittedAt"] = submitted
                    result = updater.normalize(updater.fetch_numbers([7], timeline=False)[7])
                    self.assertEqual(result["headHistoryComplete"], True)
                    self.assertEqual(result["headIntroducedAt"], LATER)
                    self.assertEqual(result["reviewDecision"], expected)


class ProviderStatusTest(unittest.TestCase):
    def test_provider_description_survives_collection(self):
        pr = raw_pr(13855)
        pr["repository"]["nameWithOwner"] = "buildbuddy-io/buildbuddy"
        check = {"__typename": "StatusContext", "id": "status", "context": "Test (darwin_arm64)",
                 "state": "ERROR", "description": "Check requires approving review",
                 "targetUrl": "https://app.buildbuddy.io"}
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = {
            "state": "FAILURE", "contexts": {"totalCount": 1, "nodes": [check], "pageInfo": {"hasNextPage": False}}}
        result = updater.normalize(pr)
        self.assertEqual(result["checks"]["contexts"][0]["description"], check["description"])
        self.assertEqual(result["checks"]["contexts"][0]["state"], "ERROR")
        self.assertIn("description", updater.CHECKS.split("... on StatusContext", 1)[1])


class ReapprovalReminderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 22, 16, 30, tzinfo=timezone.utc)
        self.raw = raw_pr(7, head=8)
        self.review = {"kind": "review", "id": "approval-7", "url": "https://github.com/google/gvisor/pull/7#review",
                       "body": "", "state": "APPROVED", "submittedAt": OLD, "updatedAt": OLD,
                       "author": {"login": "maintainer", "__typename": "User"},
                       "authorAssociation": "MEMBER", "commit": {"oid": f"{7:040x}"}}
        self.raw["reviews"]["nodes"] = [self.review]
        self.node = {"id": "pr:7", "type": "pr", "repo": "google/gvisor", "number": 7,
                     "reapproval": {"kind": "content-amendment", "method": "reviewed-source-diff",
                                    "head": f"{8:040x}", "approvedHead": f"{7:040x}", "diffSha256": "c" * 64,
                                    "summary": "Make now invokes the declared builder.",
                                    "evidence": "https://github.com/google/gvisor/compare/old..new"}}

    def plan(self, now: datetime | None = None) -> dict:
        return updater.reminder_plan(retained(self.raw), self.node, now or self.now, "tamird")

    def test_business_hours_follow_pacific_dst_and_weekdays(self) -> None:
        for stamp, due in [("2026-10-22T15:59:59+00:00", False), ("2026-10-22T16:00:00+00:00", True),
                           ("2026-10-23T00:00:00+00:00", False), ("2026-10-24T16:00:00+00:00", False),
                           ("2026-11-02T16:59:59+00:00", False), ("2026-11-02T17:00:00+00:00", True)]:
            with self.subTest(stamp=stamp):
                self.assertEqual(self.plan(datetime.fromisoformat(stamp))["status"] == "due", due)
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.plan(datetime(2026, 10, 22, 16, 30))

    def test_exact_amendment_and_maintainer_binding(self) -> None:
        plan = self.plan()
        self.assertEqual((plan["approvedHead"], plan["head"], plan["approvers"]),
                         (f"{7:040x}", f"{8:040x}", ["maintainer"]))
        self.assertIn("🚨 @maintainer", plan["body"])
        self.assertIn(f"commit/{7:040x}", plan["body"])
        self.assertIn(f"commit/{8:040x}", plan["body"])
        self.assertIn("Posted by Codex on behalf of @tamird.", plan["body"])
        for key, value in [("head", "a" * 40), ("approvedHead", "a" * 40), ("diffSha256", "bad")]:
            with self.subTest(key=key), mock.patch.dict(self.node["reapproval"], {key: value}):
                self.assertNotEqual(self.plan()["status"], "due")
        with mock.patch.dict(self.node, {"reapproval": {}}):
            self.assertEqual(self.plan()["status"], "needs-evidence")
        with mock.patch.dict(self.node, {"approvedPatch": {"head": f"{8:040x}"}}):
            self.assertEqual(self.plan()["reason"], "reviewed-patch-preserving-rebase")
        for author, association in [({"login": "tamird", "__typename": "User"}, "OWNER"),
                                    ({"login": "bot", "__typename": "Bot"}, "MEMBER"),
                                    ({"login": "outsider", "__typename": "User"}, "NONE")]:
            with self.subTest(author=author), mock.patch.dict(self.review, {"author": author, "authorAssociation": association}):
                self.assertNotEqual(self.plan()["status"], "due")

    def test_current_approval_changes_requested_draft_and_closed_stop_reminders(self) -> None:
        for state, reason in [("APPROVED", "current-head-approved"),
                              ("CHANGES_REQUESTED", "current-head-needs-source-work")]:
            current = {**self.review, "id": "new-review", "state": state, "submittedAt": LATER,
                       "author": {"login": "another-maintainer", "__typename": "User"},
                       "commit": {"oid": f"{8:040x}"}}
            with mock.patch.dict(self.raw["reviews"], {"nodes": [self.review, current]}):
                self.assertEqual(self.plan()["reason"], reason)
                current["authorAssociation"] = "NONE"
                self.assertEqual(self.plan()["status"], "due")
        for change in [{"isDraft": True}, {"state": "CLOSED"}, {"author": {"login": "other"}},
                       {"repository": {"nameWithOwner": "tamird/gvisor"}}]:
            with self.subTest(change=change), mock.patch.dict(self.raw, change):
                self.assertNotEqual(self.plan()["status"], "due")

    def test_reminder_only_rebase_proof_preserves_dismissed_review(self) -> None:
        self.review["state"] = "DISMISSED"
        self.raw["reviewDismissals"]["nodes"] = [
            {"id": "dismissal", "createdAt": LATER, "previousReviewState": "APPROVED", "review": {"id": "approval-7"}}]
        self.node["reapproval"]["kind"] = "patch-preserving-rebase"
        self.assertEqual(self.plan()["reason"], "reviewed-patch-preserving-rebase")
        self.assertEqual(self.review["state"], "DISMISSED")
        self.assertNotIn("approvedPatch", self.node)
        for key, value in [("head", "a" * 40), ("approvedHead", "bad"),
                           ("diffSha256", "bad"), ("kind", "unreviewed-rebase")]:
            with self.subTest(key=key), mock.patch.dict(self.node["reapproval"], {key: value}):
                self.assertEqual(self.plan()["status"], "needs-evidence")

    def test_outside_hours_retains_amendment_and_unknown_evidence_classification(self) -> None:
        early = datetime(2026, 10, 22, 15, 30, tzinfo=timezone.utc)
        planned = self.plan(early)
        self.assertEqual(planned["status"], "deferred")
        self.assertIn("Copybara may still import", planned["body"])
        with mock.patch.dict(self.node, {"reapproval": {}}):
            self.assertEqual(self.plan(early)["status"], "needs-evidence")

    def test_one_exact_current_approval_clears_alarm_despite_review_requests(self) -> None:
        # This alarm concerns stale approved bytes, not aggregate merge readiness.
        self.raw["reviews"]["nodes"] = [{**self.review, "submittedAt": LATER, "commit": {"oid": f"{8:040x}"}}]
        self.raw["reviewDecision"] = "REVIEW_REQUIRED"
        self.raw["reviewRequests"]["nodes"] = [{"requestedReviewer": {"__typename": "User", "login": "maintainer"}}]
        self.assertEqual(self.plan()["reason"], "current-head-approved")
        self.raw["headHistory"]["nodes"] = [
            {"createdAt": "2000-01-03T00:00:00Z", "afterCommit": {"oid": f"{8:040x}"}}]
        self.assertNotEqual(self.plan().get("reason"), "current-head-approved")

    def test_dismissed_approval_requires_original_state_evidence(self) -> None:
        self.review["state"] = "DISMISSED"
        self.assertNotEqual(self.plan()["status"], "due")
        self.raw["reviewDismissals"]["nodes"] = [
            {"id": "dismissal", "createdAt": LATER, "previousReviewState": "APPROVED", "review": {"id": "approval-7"}}]
        self.assertEqual(self.plan()["status"], "due")
        self.raw["reviewDismissals"]["nodes"][0]["previousReviewState"] = "CHANGES_REQUESTED"
        self.assertNotEqual(self.plan()["status"], "due")
        self.raw["reviewDismissals"]["pageInfo"]["hasPreviousPage"] = True
        self.assertEqual(self.plan()["reason"], "incomplete-review-or-comment-history")

    def test_comment_history_deduplicates_a_full_hour_including_head_changes(self) -> None:
        comment = {"kind": "comment", "author": {"login": "tamird", "__typename": "User"},
                   "createdAt": "2026-10-22T15:30:01Z", "body": self.plan()["body"]}
        self.raw["comments"]["nodes"] = [comment]
        self.assertEqual(self.plan()["reason"], "reminded-within-one-hour")
        comment["body"] = comment["body"].replace(f"head={8:040x}", "head=" + "e" * 40)
        self.assertEqual(self.plan()["reason"], "reminded-within-one-hour")
        comment["createdAt"] = "2026-10-22T15:30:00Z"
        self.assertEqual(self.plan()["status"], "due")
        self.raw["comments"]["pageInfo"]["hasPreviousPage"] = True
        self.assertEqual(self.plan()["status"], "due")  # The retained window covers the full hour.
        comment["createdAt"] = "2026-10-22T16:29:00Z"
        self.assertEqual(self.plan()["reason"], "incomplete-reminder-window")

    def test_post_uses_fresh_state_and_never_retries_an_ambiguous_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "github-status.json").write_text(json.dumps({"prs": [retained(self.raw)]}))
            registry = {"meta": {"repo": "google/gvisor", "owner": "tamird"}, "nodes": [self.node]}
            comments = []

            def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
                if args == ["gh", "api", "user", "--jq", ".login"]:
                    return subprocess.CompletedProcess(args, 0, "tamird\n", "")
                self.assertEqual(args[:4], ["gh", "api", "--method", "POST"])
                body = json.loads(kwargs["input"])["body"]
                comments.append(body)
                return subprocess.CompletedProcess(args, 0, json.dumps({"body": body, "user": {"login": "tamird"},
                        "html_url": "https://github.com/google/gvisor/pull/7#issuecomment-1", "created_at": self.now.isoformat()}), "")

            with mock.patch.multiple(updater, ROOT=root, REGISTRY=registry), \
                    mock.patch.object(updater, "datetime") as clock, \
                    mock.patch.object(updater, "fetch_numbers", return_value={7: self.raw}), \
                    mock.patch.object(updater.subprocess, "run", side_effect=run) as transport:
                clock.now.return_value = self.now
                updater.reapproval_reminders(post=True)
                self.assertEqual(len(comments), 1)
                self.raw["reviews"]["nodes"] = [{**self.review, "commit": {"oid": f"{8:040x}"}}]
                updater.reapproval_reminders(post=True)
                self.assertEqual(len(comments), 1)
                self.raw["reviews"]["nodes"] = [self.review]
                self.raw["headRefOid"] = "a" * 40
                self.raw["commits"]["nodes"][0]["commit"]["oid"] = "a" * 40
                updater.reapproval_reminders(post=True)
                self.assertEqual(len(comments), 1)
                self.raw["headRefOid"] = f"{8:040x}"
                self.raw["commits"]["nodes"][0]["commit"]["oid"] = f"{8:040x}"
                transport.side_effect = [subprocess.CompletedProcess([], 0, "tamird\n", ""),
                                         subprocess.TimeoutExpired("gh", 60)]
                calls = transport.call_count
                with self.assertRaises(subprocess.TimeoutExpired):
                    updater.reapproval_reminders(post=True)
                self.assertEqual(transport.call_count - calls, 2)


if __name__ == "__main__":
    unittest.main()
