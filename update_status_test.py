"""Collector history retention and reopening through the real main path."""

import copy
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
    item.update(checkedAt=OLD, importsComplete=True, imports=imports or [])
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
        self.assertEqual(self.inventory_pages, 2)
        self.assertEqual(set(self.requested), {2, 3, 4, 5, 6, 101, 103, 105})
        self.assertEqual(len(self.requested), 8)
        self.assertEqual(updater.REQUEST_LIMIT, 48)
        self.assertLess(updater.requests, 10)
        prs = {pr["number"]: pr for pr in current["prs"]}
        self.assertEqual(set(prs), {1, 2, 3, 4, 5, 6, *range(200, 300)})
        self.assertEqual(prs[1]["checkedAt"], OLD)
        self.assertEqual(prs[200], self.previous["prs"][4])
        self.assertEqual((prs[2]["status"], prs[2]["head"], prs[2]["checkedAt"]),
                         ("open", f"{202:040x}", current["checkedAt"]))
        self.assertEqual(prs[4]["status"], "merged")
        self.assertEqual(prs[4]["checkedAt"], current["checkedAt"])
        imports = {item["number"]: item for pr in prs.values() for item in pr["imports"]}
        self.assertEqual(imports[101]["checkedAt"], current["checkedAt"])
        self.assertEqual(imports[102], self.previous["prs"][0]["imports"][1])
        self.assertEqual((imports[103]["status"], imports[103]["head"]), ("open", f"{113:040x}"))
        self.assertEqual(imports[103]["checkedAt"], current["checkedAt"])
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


if __name__ == "__main__":
    unittest.main()
