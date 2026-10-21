"""Collector history retention and reopening through the real main path."""

import copy
from datetime import datetime
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


if __name__ == "__main__":
    unittest.main()
