"""Collector history retention and reopening through the real main path."""

import copy
from datetime import datetime, timedelta, timezone
import importlib
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from work_states import checks_passed


updater = importlib.import_module("update-status")
OLD = "2000-01-01T00:00:00+00:00"
LATER = "2000-01-02T00:00:00+00:00"


class FeedbackSweepTest(unittest.TestCase):
    def test_old_commit_force_push_and_merge_stay_queued_until_reviewed(self) -> None:
        review = {"id": "investigation:project-feedback", "status": "active", "lastReviewedAt": OLD}
        registry = {"investigations": [review], "nodes": []}
        old = {"url": "pr", "head": "old", "status": "open", "headCommittedAt": OLD}
        pushed = {**old, "head": "even-older"}
        merged = {**pushed, "status": "merged", "mergedAt": LATER}
        previous = {"checkedAt": OLD, "prs": [old]}
        report = updater.feedback_sweep(registry, [pushed], previous, LATER)
        self.assertEqual(report, {"lastReviewed": OLD, "dailyReviewDue": True,
                                  "changedWork": {"pr": {"head": "even-older", "status": "open"}}})
        previous = {"checkedAt": LATER, "prs": [pushed], "projectFeedbackReview": report}
        self.assertEqual(updater.feedback_sweep(registry, [pushed], previous, LATER), report)
        self.assertEqual(updater.feedback_sweep(registry, [merged], previous, LATER)["changedWork"]["pr"]["status"], "merged")
        review["lastReviewedAt"] = LATER
        previous["prs"] = [merged]
        self.assertEqual(updater.feedback_sweep(registry, [merged], previous, LATER),
                         {"lastReviewed": LATER, "dailyReviewDue": False, "changedWork": {}})


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
        "labelHistory": {"nodes": [], "pageInfo": {"hasPreviousPage": False}},
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


class ReleaseObservationTest(unittest.TestCase):
    def observe(self, wait: dict, metadata: dict) -> tuple[str | None, str]:
        with mock.patch.object(updater.urllib.request, "urlopen",
                               return_value=io.BytesIO(json.dumps(metadata).encode())) as request:
            version = updater.release_version(wait)
        self.assertEqual(request.call_args.kwargs, {"timeout": 20})
        return version, request.call_args.args[0].full_url

    def test_bcr_excludes_yanked_versions(self) -> None:
        self.assertEqual(self.observe({"module": "llvm"}, {
            "versions": ["0.8.24", "0.8.25", "0.8.26"], "yanked_versions": {"0.8.26": "regression"},
        }), ("0.8.25", "https://bcr.bazel.build/modules/llvm/metadata.json"))
        self.assertEqual(self.observe({"module": "llvm"}, {
            "versions": ["0.8.26"], "yanked_versions": {"0.8.26": "regression"},
        })[0], None)

    def test_github_uses_published_latest_release(self) -> None:
        wait = {"repository": "docker/compose"}
        release = {"tagName": "v2.40.3", "isDraft": False, "isPrerelease": False}
        with mock.patch.object(updater, "graphql", return_value={"repository": {"latestRelease": release}}) as fetch:
            self.assertEqual(updater.release_version(wait), "v2.40.3")
            self.assertEqual(fetch.call_args.args[1], {"owner": "docker", "name": "compose"})
        with mock.patch.object(updater, "graphql", return_value={"repository": {"latestRelease": None}}):
            self.assertIsNone(updater.release_version(wait))
        for fields in ({"isDraft": True}, {"isPrerelease": True}, {"tagName": ""}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                with mock.patch.object(updater, "graphql", return_value={"repository": {"latestRelease": release | fields}}):
                    updater.release_version(wait)

    def test_exact_open_head_only_and_provider_failure_clears_old_value(self) -> None:
        rows = [retained(raw_pr(number)) for number in range(1, 6)]
        nodes = [{"id": f"pr:{row['number']}", "type": "pr", "repo": "google/gvisor", "number": row["number"],
                  "releaseWait": {"head": row["head"], "module": "llvm", "checkedVersion": "0.8.24",
                                  "confirmedAt": OLD, "nextAction": "Check the new release"}} for row in rows]
        rows[1]["head"] = "a" * 40
        rows[2]["status"] = "merged"
        rows[3]["status"] = "closed"
        for failure in (OSError("BCR unavailable"), RuntimeError("GitHub query failed")):
            with self.subTest(failure=failure):
                for row in rows:
                    row["releaseVersion"] = "stale"
                with mock.patch.object(updater, "REGISTRY", {"nodes": nodes}), \
                        mock.patch.object(updater, "release_version", side_effect=[failure, "0.8.26"]) as fetch, \
                        mock.patch("sys.stderr", new_callable=io.StringIO) as diagnostic:
                    updater.observe_release_versions(rows)
                self.assertEqual(fetch.call_args_list, [mock.call(nodes[0]["releaseWait"]), mock.call(nodes[4]["releaseWait"])])
                self.assertEqual([row["releaseVersion"] for row in rows], [None, None, None, None, "0.8.26"])
                self.assertIn(f"pr:1: {failure}", diagnostic.getvalue())
        with mock.patch.object(updater, "REGISTRY", {"nodes": []}):
            updater.observe_release_versions(rows)
        self.assertEqual(["releaseVersion" in row for row in rows], [False] * 5)

    def test_malformed_metadata_is_unavailable_not_a_stale_version(self) -> None:
        for metadata in ({}, {"versions": "0.8.26", "yanked_versions": {}},
                         {"versions": [None], "yanked_versions": {}}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self.observe({"module": "llvm"}, metadata)


class IssueInventoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.nodes = [{"id": f"issue:{index}", "type": "issue", "repo": repo, "number": index}
                      for index, repo in enumerate(["buildbuddy-io/buildbuddy", "docker/compose"] * 5, 1)]
        self.rows = {f"i{index}": {"issue": {
            "url": f'https://github.com/{node["repo"]}/issues/{node["number"]}',
            "state": "CLOSED", "stateReason": "NOT_PLANNED", "closedAt": OLD, "updatedAt": LATER,
            "comments": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }} for index, node in enumerate(self.nodes)}
        self.batches = []

    def graphql(self, query: str, *, missing_issue_aliases: set[str]) -> dict:
        aliases = re.findall(r"(i\d+):repository", query)
        self.batches.append(aliases)
        self.assertLessEqual(len(aliases), updater.PR_BATCH_SIZE)
        self.assertEqual(missing_issue_aliases, set(aliases))
        return {alias: copy.deepcopy(self.rows[alias]) for alias in aliases}

    def test_all_repositories_refresh_closure_reason_and_reopening_in_batches(self) -> None:
        with mock.patch.object(updater, "REGISTRY", {"nodes": self.nodes}), \
                mock.patch.object(updater, "graphql", side_effect=self.graphql):
            closed = updater.collect_issues({})
            self.rows["i0"]["issue"].update(state="OPEN", stateReason="REOPENED", closedAt=None)
            reopened = updater.collect_issues({"issues": closed})
        self.assertEqual(self.batches, [[f"i{i}" for i in range(8)], ["i8", "i9"]] * 2)
        self.assertEqual(set(closed), {node["id"] for node in self.nodes})
        self.assertEqual((closed["issue:1"]["githubState"], closed["issue:1"]["githubStateReason"]),
                         ("closed", "NOT_PLANNED"))
        self.assertEqual((reopened["issue:1"]["githubState"], reopened["issue:1"]["closedAt"]), ("open", None))
        self.assertEqual(reopened["issue:2"]["githubState"], closed["issue:2"]["githubState"])

    def test_missing_and_relocated_issue_retains_history_while_other_records_refresh(self) -> None:
        prior = {"githubState": "closed", "githubStateReason": "COMPLETED", "updatedAt": OLD, "checkedAt": OLD}
        for row in (None, {"issue": None}, {"issue": {**self.rows["i0"]["issue"],
                                                      "url": "https://github.com/other/repo/issues/99"}}):
            with self.subTest(row=row), mock.patch.object(updater, "REGISTRY", {"nodes": self.nodes[:2]}), \
                    mock.patch.object(updater, "graphql", return_value={"i0": row, "i1": self.rows["i1"]}):
                result = updater.collect_issues({"issues": {"issue:1": prior}})
                self.assertEqual({key: result["issue:1"][key] for key in prior}, prior)
                self.assertIn("Last observation retained", result["issue:1"]["unavailable"])
                self.assertEqual(result["issue:2"]["githubStateReason"], "NOT_PLANNED")
                unknown = updater.collect_issues({})["issue:1"]
                self.assertEqual(unknown["githubState"], "unknown")
                self.assertIn("No provider observation", unknown["unavailable"])
        with mock.patch.object(updater, "REGISTRY", {"nodes": self.nodes[:1]}), \
                mock.patch.object(updater, "graphql", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "inventory is incomplete"):
                updater.collect_issues({})

    def test_issue_discussion_reports_new_edits_and_truncation_once(self) -> None:
        node = self.nodes[0]
        issue = self.rows["i0"]["issue"]
        original = {"url": issue["url"] + "#issuecomment-1", "updatedAt": OLD, "author": {"login": "reporter"}}
        edited = {**original, "updatedAt": "2000-01-03T00:00:00Z"}
        new = {**edited, "url": issue["url"] + "#issuecomment-2"}
        with mock.patch.object(updater, "REGISTRY", {"nodes": [node]}), mock.patch.object(updater, "graphql", self.graphql):
            issue["comments"]["nodes"] = [original]
            before = updater.collect_issues({})
            self.assertEqual(list(updater.issue_discussions(before, {})), [{"issue": node["id"], "latest": original}])
            before[node["id"]]["checkedAt"] = LATER
            issue["comments"] = {"nodes": [new, edited], "pageInfo": {"hasNextPage": True}}
            after = updater.collect_issues({"issues": before})
        self.assertEqual(list(updater.issue_discussions(after, {"issues": before})), [
            {"issue": node["id"], "comment": new}, {"issue": node["id"], "comment": edited},
            {"issue": node["id"], "gap": issue["url"]}])
        self.assertEqual(list(updater.issue_discussions(after, {"issues": after})), [])

    def test_partial_gh_exit_retains_missing_issue_and_refreshes_sibling(self) -> None:
        prior = {"githubState": "closed", "updatedAt": OLD, "checkedAt": OLD}
        response = {"data": {"i0": {"issue": None}, "i1": self.rows["i1"],
                             "rateLimit": {"cost": 1, "remaining": 4000}},
                    "errors": [{"type": "NOT_FOUND", "path": ["i0", "issue"],
                                "message": "Could not resolve to an Issue"}]}
        completed = subprocess.CompletedProcess([], 1, json.dumps(response), "gh: Could not resolve to an Issue")
        with mock.patch.object(updater, "REGISTRY", {"nodes": self.nodes[:2]}), \
                mock.patch.object(updater, "requests", 0), \
                mock.patch.object(updater, "query_cost", 0), \
                mock.patch.object(updater, "rate_remaining", None), \
                mock.patch.object(updater.subprocess, "run", return_value=completed) as run:
            result = updater.collect_issues({"issues": {"issue:1": prior}})
            self.assertEqual({key: result["issue:1"][key] for key in prior}, prior)
            self.assertIn("Last observation retained", result["issue:1"]["unavailable"])
            self.assertEqual(result["issue:2"]["updatedAt"], LATER)
            self.assertEqual(result["issue:2"]["githubState"], "closed")
            self.assertEqual((updater.requests, updater.query_cost, updater.rate_remaining), (1, 1, 4000))
            run.assert_called_once()

        for error in ({"type": "FORBIDDEN", "path": ["i0", "issue"]},
                      {"type": "NOT_FOUND", "path": ["unknown", "issue"]},
                      {"type": "NOT_FOUND", "path": ["i0", "pullRequest"]},
                      {"type": "NOT_FOUND", "path": ["i0"]}):
            with self.subTest(error=error), mock.patch.object(updater, "REGISTRY", {"nodes": self.nodes[:2]}), \
                    mock.patch.object(updater, "requests", 0), \
                    mock.patch.object(updater.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 1, json.dumps({**response, "errors": [error]}), "gh: query failed")) as run:
                with self.assertRaisesRegex(RuntimeError, "GitHub query failed"):
                    updater.collect_issues({})
                run.assert_called_once()


class MergeabilityBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [raw_pr(number) for number in range(1, 18)]
        for row in self.rows:
            row["mergeable"] = "UNKNOWN"
        self.batches = []

    def graphql(self, query: str) -> dict:
        numbers = [int(n) for n in re.findall(r"p(\d+):pullRequest", query)]
        self.batches.append(numbers)
        updater.requests += 1
        if len(numbers) > updater.PR_BATCH_SIZE:
            raise updater.GitHubTransportError("HTTP 502 for oversized mergeability query")
        return {"repository": {f"p{n}": {**raw_pr(n),
                "mergeable": "CONFLICTING" if n == 13 else "MERGEABLE"} for n in numbers}}

    def test_large_unknown_inventory_uses_bounded_head_checked_batches(self) -> None:
        with mock.patch.object(updater, "requests", 0), \
                mock.patch.object(updater, "graphql", side_effect=self.graphql):
            result = updater.recheck_unknown_mergeability(self.rows)
        self.assertEqual(result, {"status": "completed", "numbers": list(range(1, 18))})
        self.assertEqual(self.batches, [list(range(1, 9)), list(range(9, 17)), [17]])
        self.assertEqual([row["number"] for row in self.rows if row["mergeable"] == "CONFLICTING"], [13])
        self.assertTrue(all("_mergeabilityRetrievedAt" in row for row in self.rows))

    def test_late_transport_failure_preserves_completed_batch_only(self) -> None:
        def fail_second(query: str) -> dict:
            if self.batches:
                raise updater.GitHubTransportError("HTTP 502")
            return self.graphql(query)

        with mock.patch.object(updater, "requests", 0), \
                mock.patch.object(updater, "graphql", side_effect=fail_second):
            result = updater.recheck_unknown_mergeability(self.rows)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual([row["mergeable"] for row in self.rows], ["MERGEABLE"] * 8 + ["UNKNOWN"] * 9)
        self.assertEqual([row["number"] for row in self.rows if "_mergeabilityRetrievedAt" in row], list(range(1, 9)))

    def test_request_budget_stops_before_next_batch(self) -> None:
        with mock.patch.object(updater, "requests", updater.REQUEST_LIMIT - 1), \
                mock.patch.object(updater, "graphql", side_effect=self.graphql):
            result = updater.recheck_unknown_mergeability(self.rows)
            self.assertEqual(updater.requests, updater.REQUEST_LIMIT)
        self.assertEqual(result["status"], "budget-exhausted")
        self.assertEqual(self.batches, [list(range(1, 9))])
        self.assertEqual([row["mergeable"] for row in self.rows], ["MERGEABLE"] * 8 + ["UNKNOWN"] * 9)

    def test_recheck_rejects_changed_export_and_preserves_all_copies(self) -> None:
        first = retained(self.rows[12])
        second = copy.deepcopy(first)
        with mock.patch.object(updater, "graphql", side_effect=self.graphql), mock.patch.object(updater, "requests", 0):
            updater.recheck_snapshot_mergeability([self.rows[12]], [first, second], "google/gvisor")
            self.assertEqual(self.batches, [[13]])
            self.assertEqual(first["mergeabilityObservation"]["state"], "CONFLICTING")
            self.assertEqual(first, second)
            for row in (first, second):
                row["mergeable"] = "UNKNOWN"
            self.rows[12]["mergeable"] = "UNKNOWN"
            second["head"] = "a" * 40
            with self.assertRaisesRegex(RuntimeError, "changed during mergeability recheck"):
                updater.recheck_snapshot_mergeability([self.rows[12]], [first, second], "google/gvisor")

    def test_reread_clocks_pass_the_snapshot_validator(self) -> None:
        rows = [retained(raw) for raw in self.rows]
        previous = {"state": "CONFLICTING", "head": rows[0]["head"], "base": rows[0]["base"], "checkedAt": OLD}
        rows[0]["mergeabilityObservation"] = previous

        def recheck(query: str) -> dict:
            result = self.graphql(query)
            result["repository"]["p1"]["mergeable"] = "UNKNOWN"
            return result

        with mock.patch.object(updater, "requests", updater.REQUEST_LIMIT - 1), \
                mock.patch.object(updater, "graphql", side_effect=recheck):
            result = updater.recheck_snapshot_mergeability(self.rows, rows, "google/gvisor")
            self.assertEqual(updater.requests, updater.REQUEST_LIMIT)
        self.assertEqual(result["status"], "budget-exhausted")
        self.assertEqual(self.batches, [list(range(1, 9))])
        self.assertEqual([row["mergeable"] for row in rows], ["UNKNOWN"] + ["MERGEABLE"] * 7 + ["UNKNOWN"] * 9)
        self.assertEqual(rows[0]["mergeabilityObservation"], previous)
        for row in rows:
            self.assertEqual(row["checkedAt"], row["mergeabilityCheckedAt"])
        self.assertEqual([row["number"] for row in rows if row["checkedAt"] != OLD], list(range(1, 9)))

        # Exercise the same paired metadata validator the collector uses to publish.
        snapshot = json.loads((updater.ROOT / "github-status.json").read_bytes())
        snapshot.update(prs=rows, workStates={}, checkedAt=rows[0]["checkedAt"])
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "snapshot.json"
            candidate.write_text(json.dumps(snapshot))
            subprocess.run(["node", updater.ROOT / "metadata-preflight.mjs", updater.ROOT, candidate],
                           check=True, capture_output=True, text=True, timeout=30)


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

    def test_main_publishes_fresh_release_observation_and_survives_unavailability(self) -> None:
        self.registry["nodes"].append({"id": "pr:3", "type": "pr", "repo": "google/gvisor", "number": 3,
                                       "releaseWait": {"head": f"{3:040x}", "module": "llvm"}})
        with mock.patch.object(updater, "release_version", side_effect=[
            "0.8.26", updater.http.client.IncompleteRead(b"partial", 20),
        ]):
            first = self.collect()
            second = self.collect()
        self.assertEqual(next(pr for pr in first["prs"] if pr["number"] == 3)["releaseVersion"], "0.8.26")
        self.assertIsNone(next(pr for pr in second["prs"] if pr["number"] == 3)["releaseVersion"])
        self.assertEqual([pr["number"] for pr in second["prs"]], [pr["number"] for pr in first["prs"]])

    def test_explicit_external_review_subscription_and_event_deduplication(self) -> None:
        watch = {"owner": "root", "reviewers": ["hostinet_delivery", "node_tls_sni", "work_map_current"]}
        self.registry["nodes"].append({"id": "pr:500", "type": "pr", "repo": "google/gvisor",
                                       "number": 500, "reviewWatch": watch})
        self.raw[500] = raw_pr(500, author="EtiennePerot")
        current = self.collect()
        self.assertIn(500, self.requested)
        pr = next(pr for pr in current["prs"] if pr["number"] == 500)
        with mock.patch.object(updater, "REGISTRY", self.registry):
            events = updater.review_watch_report(current["prs"], self.previous)
            self.assertEqual([(event["number"], event["head"], event["owner"]) for event in events],
                             [(500, pr["head"], "root")])
            self.assertEqual(updater.review_watch_report(current["prs"], current), [])
            changed = copy.deepcopy(pr)
            changed["head"] = "a" * 40
            self.assertEqual(updater.review_watch_report([changed], current)[0]["head"], "a" * 40)
            review = {"id": "external-review", "kind": "review", "body": "The measured CPU includes GC.",
                      "updatedAt": LATER, "author": {"login": "review-bot", "__typename": "Bot"}}
            changed = copy.deepcopy(pr)
            changed["feedback"]["items"].append(review)
            event = updater.review_watch_report([changed], current)[0]
            self.assertEqual([item["id"] for item in event["feedback"]], ["external-review"])
            seen = {"prs": [copy.deepcopy(changed)]}
            self.assertEqual(updater.review_watch_report([changed], seen), [])
            review["body"] = "Updated GC finding."
            self.assertEqual(len(updater.review_watch_report([changed], seen)), 1)
            review["author"]["login"] = "tamird"
            self.assertEqual(updater.review_watch_report([changed], current), [])
            review["author"]["login"] = "external"
            review["body"] = ""
            self.assertEqual(updater.review_watch_report([changed], current), [])
            changed = copy.deepcopy(pr)
            changed["status"] = "merged"
            self.assertEqual(updater.review_watch_report([changed], current)[0]["status"], "merged")
            self.assertEqual(updater.review_watch_report([changed], {"prs": [changed]}), [])

    def test_investigation_tracks_external_review_without_a_delivery_row(self) -> None:
        self.registry["investigations"] = [{"id": "investigation:external-reviews", "reviewWatch": {
            "repo": "google/gvisor", "numbers": [500], "owner": "Netstack", "reviewers": ["tamird"],
        }}]
        self.raw[500] = raw_pr(500, author="outside-author")
        current = self.collect()
        self.assertNotIn(500, [pr["number"] for pr in current["prs"]])
        pr, = current["reviewPRs"]
        self.assertEqual(pr["number"], 500)
        with mock.patch.object(updater, "REGISTRY", self.registry):
            self.assertEqual([(event["number"], event["owner"]) for event in
                              updater.review_watch_report([pr], self.previous)], [(500, "Netstack")])
            self.assertEqual(updater.review_watch_report([pr], current), [])

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

    def test_required_repositories_and_issues_get_budget_before_unknown_rechecks(self) -> None:
        second_repo = "other/project"
        self.registry["meta"].update(repositories={"google/gvisor": "copybara", second_repo: "none"},
                                     registeredOnlyRepositories=[second_repo])
        self.registry["nodes"].extend([
            {"id": "other:17", "type": "pr", "repo": second_repo, "number": 17},
            {"id": "issue:19", "type": "issue", "repo": second_repo, "number": 19},
        ])
        self.raw[3]["mergeable"] = "UNKNOWN"
        self.raw[17] = raw_pr(17)
        self.raw[17]["repository"]["nameWithOwner"] = second_repo
        self.raw[17]["url"] = f"https://github.com/{second_repo}/pull/17"
        regular_query = self.graphql

        def query(text: str, variables: dict | None = None, *, missing_issue_aliases: set[str] | None = None) -> dict:
            if missing_issue_aliases is not None:
                updater.requests += 1
                self.assertEqual(missing_issue_aliases, {"i0"})
                return {"i0": {"issue": {"url": f"https://github.com/{second_repo}/issues/19", "state": "OPEN",
                                         "stateReason": None, "closedAt": None, "updatedAt": LATER,
                                         "comments": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}}
            return regular_query(text, variables)

        # Two inventory pages, two PR detail queries, the second repository,
        # and its issue use all six requests. There is no optional read left.
        with mock.patch.object(self, "graphql", side_effect=query), mock.patch.object(updater, "REQUEST_LIMIT", 6):
            snapshot = self.collect()
        self.assertEqual(snapshot["limits"]["requests"], 6)
        self.assertEqual(snapshot["issues"]["issue:19"]["githubState"], "open")
        prs = {pr["number"]: pr for pr in snapshot["prs"]}
        self.assertEqual(prs[17]["repo"], second_repo)
        self.assertEqual(prs[3]["mergeable"], "UNKNOWN")
        self.assertEqual(snapshot["limits"]["repositories"]["google/gvisor"]["mergeabilityRecheck"],
                         {"status": "budget-exhausted", "numbers": [3]})

    def test_required_failure_or_footer_race_preserves_snapshot(self) -> None:
        for failure in ("inventory", "required", "footer"):
            with self.subTest(failure=failure):
                self.failure = failure
                with self.assertRaises(RuntimeError):
                    self.collect()
                self.assertEqual(self.path.read_bytes(), self.original_bytes)
                self.assertFalse((self.root / ".github-status.json.tmp").exists())

    def test_large_check_inventory_keeps_following_feedback_in_cli_logs(self) -> None:
        # A retained workflow emitted a 136 KiB aggregate line. gh run view's
        # default bufio.Scanner then omitted that line and the feedback tail.
        observation = {"repo": "google/gvisor", "source": 15931, "number": 15931,
                       "sourceHead": "5c36118d69d861a79bd6f97df9b77e0a64d44ddf",
                       "failedChecks": [{"name": "feedback/copybara", "state": "FAILURE",
                                         "description": "Internal check diagnostic " * 20}]}
        blocker = {**observation, "url": "https://github.com/google/gvisor/pull/15931", "scope": "source",
                   "head": observation["sourceHead"], "branchHead": None, "base": "f" * 40,
                   "mergeable": "CONFLICTING", "checkedAt": OLD, "mergeabilityCheckedAt": OLD,
                   "sourceBranchMismatch": False, "followup": {"owner": "root"}}
        report = {"checkFailures": [observation] * 100, "changedChecks": [], "mergeabilityIssues": [blocker]}
        self.assertGreater(len(json.dumps(report).encode()), 64 * 1024)
        comment = {"id": "inline-new", "url": "https://github.com/google/gvisor/pull/3#discussion_r1",
                   "body": "The sysctl write can return EACCES.", "createdAt": LATER, "updatedAt": LATER,
                   "author": {"login": "reviewer", "__typename": "User"}}
        self.raw[3]["reviewThreads"].update(totalCount=1, nodes=[
            {"id": "thread-new", "isResolved": False, "isOutdated": False, "path": "setup.cc", "line": 1,
             "comments": {"nodes": [comment], "pageInfo": {"hasPreviousPage": False}}}])
        with mock.patch.object(updater, "check_report", return_value=report), mock.patch("builtins.print") as output:
            current = self.collect()
        lines = [call.args[0] for call in output.call_args_list]
        self.assertLess(max(len(line.encode()) for line in lines), 64 * 1024)
        events = [json.loads(line) for line in lines if line.startswith("{")]
        self.assertEqual([item for event in events for item in event.get("checkFailures", [])],
                         report["checkFailures"])
        self.assertEqual(current["checkFailures"], report["checkFailures"])
        self.assertIn({"changedChecks": []}, events)
        feedback = [item for event in events for item in event.get("feedback", [])]
        self.assertEqual([item["id"] for item in feedback], [comment["id"]])
        self.assertEqual(next(pr for pr in current["prs"] if pr["number"] == 3)["threads"]["unresolved"], 1)
        handoff = [event for event in events if "reviewerHandoff" in event]
        self.assertEqual(handoff[0]["reviewerHandoff"][0]["id"], comment["id"])
        self.assertLess(events.index(handoff[0]), next(i for i, event in enumerate(events) if "checkFailures" in event))
        self.assertEqual(current["reviewerHandoff"], handoff[0]["reviewerHandoff"])
        blockers = [event for event in events if "mergeabilityHandoff" in event]
        self.assertEqual(current["mergeabilityHandoff"], blockers[0]["mergeabilityHandoff"])
        self.assertLess(events.index(handoff[0]), events.index(blockers[0]))
        self.assertLess(events.index(blockers[0]), next(i for i, event in enumerate(events) if "checkFailures" in event))

    def test_later_repository_failure_retains_provisional_human_handoff_only(self) -> None:
        self.registry["meta"]["repositories"] = {
            "google/gvisor": "copybara", "buildbuddy-io/buildbuddy": "none"}
        self.registry["nodes"].append({"id": "pr:3", "type": "pr", "repo": "google/gvisor",
                                       "number": 3, "followup": {"owner": "root"}})
        comment = {"id": "reviewer-question", "url": self.raw[3]["url"] + "#issuecomment-1",
                   "body": "Does this preserve the original error?", "createdAt": LATER, "updatedAt": LATER,
                   "author": {"login": "reviewer", "__typename": "User"}}
        self.raw[3]["comments"]["nodes"] = [comment]
        collect_repository = updater.collect_repository

        def fail_later(repo: str, policy: str, previous: dict) -> tuple:
            if repo == "buildbuddy-io/buildbuddy":
                raise RuntimeError("Open PR inventory changed during retrieval")
            return collect_repository(repo, policy, previous)

        with mock.patch.object(updater, "collect_repository", side_effect=fail_later), \
                mock.patch("builtins.print") as output, \
                self.assertRaisesRegex(RuntimeError, "inventory changed"):
            self.collect()
        events = [json.loads(call.args[0]) for call in output.call_args_list if call.args[0].startswith("{")]
        provisional, = [event for event in events if "reviewerHandoffProvisional" in event]
        record, = provisional["reviewerHandoffProvisional"]
        self.assertEqual((record["id"], record["head"], record["owner"], record["requiresLiveRecheck"]),
                         (comment["id"], self.raw[3]["headRefOid"], "root", True))
        self.assertTrue(next(call.kwargs["flush"] for call in output.call_args_list
                             if "reviewerHandoffProvisional" in call.args[0]))
        self.assertFalse(any("reviewerHandoff" in event for event in events))
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assertFalse((self.root / ".github-status.json.tmp").exists())

    def test_reviewer_handoff_tracks_latest_reply_and_top_level_events(self) -> None:
        pr = retained(raw_pr(5))
        finding = {"id": "finding", "kind": "inline", "threadId": "thread", "isResolved": False,
                   "isOutdated": False, "body": "[🤖] Recycled allocation is slower.", "createdAt": OLD,
                   "updatedAt": OLD, "url": pr["url"] + "#discussion_r1",
                   "author": {"login": "reviewer", "__typename": "User"}}
        pr["feedback"]["items"] = [finding]
        pr["threads"] = {"complete": True, "total": 1, "unresolved": 1}
        self.registry["nodes"][0]["followup"] = {"owner": "root"}
        with mock.patch.object(updater, "REGISTRY", self.registry):
            first = updater.reviewer_handoff([pr], {})
            self.assertEqual((first[0]["owner"], first[0]["newOrChanged"], first[0]["awaitingAuthor"]),
                             ("root", True, True))
            previous = {"prs": [copy.deepcopy(pr)], "reviewerHandoff": first}
            self.assertFalse(updater.reviewer_handoff([pr], previous)[0]["newOrChanged"])
            bot = {**finding, "id": "bot", "createdAt": LATER,
                   "author": {"login": "review-bot", "__typename": "Bot"}}
            pr["feedback"]["items"].append(bot)
            self.assertTrue(updater.reviewer_handoff([pr], previous)[0]["awaitingAuthor"])
            binding = {field: pr.get(field) for field in (
                "reviewDecision", "approvedHead", "approvedAt", "headCommittedAt", "headIntroducedAt",
                "headHistoryComplete", "threads", "feedback")}
            binding["sourceHead"] = pr["head"]
            self.registry["nodes"][0]["followup"]["attentionDispositions"] = [
                {"reason": "author-feedback", "binding": copy.deepcopy(binding)}]
            self.assertEqual(updater.reviewer_handoff([pr], previous), [])
            reply = {**finding, "id": "reply", "createdAt": LATER, "updatedAt": LATER,
                     "body": "I am measuring the affected path.", "author": {"login": "tamird", "__typename": "User"}}
            pr["feedback"]["items"].append(reply)
            self.assertEqual(updater.reviewer_handoff([pr], previous), [])
            previous = {"prs": [copy.deepcopy(pr)]}
            followup = {**finding, "id": "followup", "createdAt": "2000-01-03T00:00:00+00:00",
                        "updatedAt": "2000-01-03T00:00:00+00:00", "body": "Please include cold pages."}
            pr["feedback"]["items"].append(followup)
            self.assertEqual([item["id"] for item in updater.reviewer_handoff([pr], previous)], ["followup"])
            followup["isResolved"] = True
            self.assertEqual(updater.reviewer_handoff([pr], previous), [])
            comment = {**followup, "id": "comment", "kind": "comment", "url": pr["url"] + "#issuecomment-1"}
            pr["feedback"]["items"].append(comment)
            self.assertEqual([item["id"] for item in updater.reviewer_handoff([pr], previous)], ["comment"])
            continued = {"prs": [copy.deepcopy(pr)], "reviewerHandoff": updater.reviewer_handoff([pr], previous)}
            pending = updater.reviewer_handoff([pr], continued)
            self.assertEqual([item["id"] for item in pending], ["comment"])
            self.assertFalse(pending[0]["newOrChanged"])
            incomplete = copy.deepcopy(pr)
            incomplete["feedback"] = {"complete": False, "items": []}
            retained_handoff = updater.reviewer_handoff([incomplete], continued)
            self.assertEqual([item["id"] for item in retained_handoff], ["comment"])
            self.assertFalse(retained_handoff[0]["feedbackComplete"])
            self.registry["nodes"][0]["followup"]["actionableComment"] = {
                "head": pr["head"], "commentId": comment["id"], "url": comment["url"]}
            assigned = {"prs": [copy.deepcopy(pr)], "reviewerHandoff": updater.reviewer_handoff([pr], continued)}
            self.assertTrue(assigned["reviewerHandoff"][0]["assigned"])
            del self.registry["nodes"][0]["followup"]["actionableComment"]
            self.assertEqual(updater.reviewer_handoff([pr], assigned), [])
            binding["feedback"] = copy.deepcopy(pr["feedback"])
            self.registry["nodes"][0]["followup"]["attentionDispositions"][0]["binding"] = copy.deepcopy(binding)
            self.assertEqual(updater.reviewer_handoff([pr], continued), [])
            pr["feedback"]["items"].append({**comment, "kind": "review", "state": "COMMENTED",
                                              "id": "top-review", "body": "Please also measure another case."})
            outstanding = updater.reviewer_handoff([pr], continued)
            resumed = {"prs": [copy.deepcopy(pr)], "reviewerHandoff": outstanding}
            self.assertIn("top-review", [item["id"] for item in updater.reviewer_handoff([pr], resumed)])
            pr["feedback"]["items"][-1]["state"] = "DISMISSED"
            self.assertNotIn("top-review", [item["id"] for item in updater.reviewer_handoff([pr], resumed)])
            pr["feedback"]["items"].pop()
            comment["author"] = {"login": "bot", "__typename": "Bot"}
            self.assertEqual(updater.reviewer_handoff([pr], previous), [])
            comment["author"] = finding["author"]
            pr["status"] = "merged"
            self.assertEqual(updater.reviewer_handoff([pr], previous), [])

    def test_import_reviewer_handoff_routes_to_source_owner(self) -> None:
        source = retained(raw_pr(5))
        target = linked(imported(105, 5), 5)
        target["feedback"]["items"] = [{"id": "import-comment", "kind": "comment", "body": "Please fix the export.",
            "updatedAt": LATER, "url": target["url"] + "#issuecomment-1",
            "author": {"login": "reviewer", "__typename": "User"}}]
        source["imports"] = [target]
        self.registry["nodes"][0]["followup"] = {"owner": "root"}
        with mock.patch.object(updater, "REGISTRY", self.registry):
            record = updater.reviewer_handoff([source], {})[0]
            self.assertEqual((record["number"], record["sourceNumber"], record["owner"]), (105, 5, "root"))
            source["status"] = "merged"
            self.assertEqual(updater.reviewer_handoff([source], {}), [])

    def test_review_body_and_edited_inline_survive_an_answer_to_another_message(self) -> None:
        pr = retained(raw_pr(5))
        finding = {"id": "inline", "kind": "inline", "threadId": "t", "isResolved": False,
                   "body": "Fix inline Y.", "createdAt": OLD, "updatedAt": OLD,
                   "url": pr["url"] + "#discussion_r1", "pullRequestReview": {"id": "review"},
                   "author": {"login": "reviewer", "__typename": "User"}}
        review = {"id": "review", "kind": "review", "body": "Also fix X.", "state": "COMMENTED",
                  "updatedAt": OLD, "url": pr["url"] + "#pullrequestreview-1", "author": finding["author"]}
        pr["feedback"]["items"] = [finding, review]
        with mock.patch.object(updater, "REGISTRY", self.registry):
            previous = {"prs": [copy.deepcopy(pr)], "reviewerHandoff": updater.reviewer_handoff([pr], {})}
            pr["feedback"]["items"].append({**finding, "id": "reply", "createdAt": LATER, "updatedAt": LATER,
                "body": "Fixed Y.", "author": {"login": "tamird", "__typename": "User"}})
            remaining = updater.reviewer_handoff([pr], previous)
            self.assertEqual([item["id"] for item in remaining], ["review"])
            previous = {"prs": [copy.deepcopy(pr)], "reviewerHandoff": remaining}
            finding.update(body="Y also fails for cold pages.", updatedAt="2000-01-03T00:00:00+00:00")
            remaining = updater.reviewer_handoff([pr], previous)
            self.assertEqual({item["id"] for item in remaining}, {"review", "inline"})
            self.assertFalse(next(item for item in remaining if item["id"] == "inline")["awaitingAuthor"])
            previous = {"prs": [copy.deepcopy(pr)], "reviewerHandoff": remaining}
            self.assertEqual({item["id"] for item in updater.reviewer_handoff([pr], previous)}, {"review", "inline"})

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

    def test_registered_only_refreshes_exact_pairs_without_discovery(self) -> None:
        repo = "nodejs/node"
        self.registry["meta"]["repositories"] = {"google/gvisor": "copybara", repo: "none"}
        self.registry["meta"]["registeredOnlyRepositories"] = [repo]
        for number in (66835, 66874):
            self.registry["nodes"].append({"id": f"pr:{repo}:{number}", "type": "pr", "repo": repo, "number": number})
        raw = {number: raw_pr(number, "MERGED" if number == 66835 else "OPEN", head=number + 1)
               for number in (66835, 66874)}
        for number, row in raw.items():
            row.update(repository={"nameWithOwner": repo}, url=f"https://github.com/{repo}/pull/{number}")
            prior = retained(row)
            prior.update(status="closed", head="a" * 40, importPolicy="none")
            self.previous["prs"].append(prior)
        unrelated = copy.deepcopy(self.previous["prs"][-1])
        unrelated.update(number=99999, status="open")
        self.previous["prs"].append(unrelated)
        primary_graphql = self.graphql
        node_requests = []

        def graphql(query: str, variables: dict | None = None) -> dict:
            self.assertNotEqual((variables or {}).get("owner"), "nodejs", "Registered-only mode must not discover PRs")
            if 'owner:"nodejs"' in query:
                numbers = [int(number) for number in re.findall(r"p(\d+):pullRequest", query)]
                node_requests.extend(numbers)
                self.assertEqual(numbers, [66835, 66874])
                return {"repository": {f"p{number}": copy.deepcopy(raw[number]) for number in numbers}}
            return primary_graphql(query, variables)

        with mock.patch.object(self, "graphql", side_effect=graphql):
            current = self.collect()
        self.assertEqual(node_requests, [66835, 66874])
        records = {row["number"]: row for row in current["prs"] if row["repo"] == repo}
        self.assertEqual(set(records), {66835, 66874})
        self.assertEqual(records[66835]["status"], "merged")
        self.assertEqual(records[66874]["status"], "open")
        self.assertEqual(records[66874]["head"], raw[66874]["headRefOid"])
        self.assertEqual(records[66874]["imports"], [])
        self.assertNotEqual(records[66874]["checkedAt"], OLD)
        inventory = current["limits"]["repositories"][repo]
        self.assertEqual((inventory["scope"], inventory["registeredCount"], inventory["openInventoryTotal"]),
                         ("registered-only", 2, None))

    def test_registered_only_requires_direct_review_policy(self) -> None:
        self.registry["meta"]["registeredOnlyRepositories"] = ["google/gvisor"]
        with self.assertRaisesRegex(ValueError, "registered-only"):
            self.collect()
        self.assertEqual(self.path.read_bytes(), self.original_bytes)

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
    def test_unchanged_conflict_and_branch_mismatch_remain_owned(self) -> None:
        current = retained(raw_pr(15376))
        current.update(mergeable="CONFLICTING", branchHead="b" * 40)
        previous = {"prs": [copy.deepcopy(current)], "workStates": {
            "pr:15376": {"conflicts": {"scope": current["head"], "since": OLD}}}}
        registry = {"nodes": [{"type": "pr", "repo": "google/gvisor", "number": 15376,
                              "followup": {"owner": "root", "sourceHead": "b" * 40,
                                           "nextAction": "Wait for provider synchronization; no repush."}}]}
        with mock.patch.object(updater, "REGISTRY", registry):
            for _ in range(2):
                report = updater.check_report([current], previous)
                self.assertEqual(report["changedMergeability"], [])
                handoff = updater.mergeability_handoff(report, previous)
                self.assertEqual(len(handoff), 1)
                self.assertEqual(handoff[0]["owner"], "root")
                self.assertEqual(handoff[0]["conflictObservedSince"], OLD)
                self.assertIn("independently check both", handoff[0]["action"])
                self.assertIn("Wait for provider", handoff[0]["recordedFollowup"]["nextAction"])
                previous["mergeabilityHandoff"] = handoff
            # Provider MERGEABLE cannot clear a still-divergent fork branch.
            current["mergeable"] = "MERGEABLE"
            self.assertEqual(len(updater.mergeability_handoff(updater.check_report([current], previous), previous)), 1)
            current["head"] = current["branchHead"]
            self.assertEqual(updater.mergeability_handoff(updater.check_report([current], previous), previous), [])
            current.update(mergeable="CONFLICTING", status="closed")
            self.assertEqual(updater.mergeability_handoff(updater.check_report([current], previous), previous), [])

    def test_import_handoff_requires_current_source_identity(self) -> None:
        current = retained(raw_pr(7), [linked(imported(107, 7), 7)])
        current["imports"][0]["mergeable"] = "CONFLICTING"
        handoff = updater.mergeability_handoff(updater.check_report([current], {}), {})
        self.assertEqual([(row["number"], row["scope"]) for row in handoff], [(107, "current-source-import")])
        current["imports"][0]["sourceHead"] = "a" * 40
        report = updater.check_report([current], {})
        self.assertEqual(len(report["mergeabilityIssues"]), 1)
        self.assertEqual(updater.mergeability_handoff(report, {}), [])

    def test_branch_ref_mismatch_retains_raw_checks_and_conflict(self) -> None:
        raw = raw_pr(15153)
        raw["mergeable"] = "CONFLICTING"
        raw["headRef"] = {"name": raw["headRefName"], "target": {"oid": "b" * 40}}
        current = retained(raw)
        self.assertEqual(current["head"], raw["headRefOid"])
        self.assertEqual(current["branchHead"], "b" * 40)
        row = updater.check_report([current], {})["mergeabilityIssues"][0]
        self.assertTrue(row["sourceBranchMismatch"])
        self.assertEqual(row["mergeable"], "CONFLICTING")
        previous = {"prs": [copy.deepcopy(current)]}
        current["branchHead"] = current["head"]
        self.assertFalse(updater.check_report([current], previous)["changedMergeability"][0]["sourceBranchMismatch"])
        current.update(mergeable="MERGEABLE", branchHead="b" * 40)
        previous = {"prs": [copy.deepcopy(current)]}
        current["head"] = current["branchHead"]
        report = updater.check_report([current], previous)
        self.assertEqual(report["mergeabilityIssues"], [])
        self.assertFalse(report["changedMergeability"][0]["sourceBranchMismatch"])
        raw["headRef"] = None
        self.assertIsNone(retained(raw)["branchHead"])
        raw["headRef"] = {"name": "wrong-branch", "target": {"oid": "b" * 40}}
        with self.assertRaisesRegex(RuntimeError, "invalid source branch identity"):
            retained(raw)

    def test_conflicts_unknown_and_resolution_remain_visible_without_failed_checks(self) -> None:
        source = retained(raw_pr(3))
        old_import = linked(imported(103, 3), 3)
        source["imports"] = [old_import]
        source["mergeable"] = "CONFLICTING"
        old_import["mergeable"] = "UNKNOWN"
        previous = {"prs": [copy.deepcopy(source)]}
        report = updater.check_report([source], previous)
        self.assertEqual([(item["number"], item["mergeable"]) for item in report["mergeabilityIssues"]],
                         [(3, "CONFLICTING"), (103, "UNKNOWN")])
        self.assertEqual(report["changedMergeability"], [])
        source["head"] = "b" * 40
        report = updater.check_report([source], previous)
        self.assertEqual(report["changedMergeability"][0]["head"], source["head"])
        self.assertEqual(report["mergeabilityIssues"][1]["scope"], "older-source-import")
        source["mergeable"] = "MERGEABLE"
        old_import["status"] = "closed"
        report = updater.check_report([source], previous)
        self.assertEqual(report["mergeabilityIssues"], [])
        self.assertEqual([(item["number"], item["status"], item["mergeable"])
                          for item in report["changedMergeability"]],
                         [(3, "open", "MERGEABLE"), (103, "closed", "UNKNOWN")])

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

    def test_terminal_source_keeps_failed_import_history_out_of_current_attention(self) -> None:
        source = retained(raw_pr(3))
        old_import = linked(imported(103, 3), 3)
        failure = {"id": "generate-1", "kind": "check-run", "name": "generate", "state": "FAILURE"}
        old_import["checks"] = {"state": "FAILURE", "total": 1, "complete": True, "contexts": [failure]}
        old_import["mergeable"] = "CONFLICTING"
        source["imports"] = [old_import]
        previous = {"prs": [copy.deepcopy(source)]}
        for status in ("merged", "closed"):
            with self.subTest(status=status):
                source["status"] = status
                retained_source = copy.deepcopy(source)
                report = updater.check_report([source], previous)
                for field in ("checkFailures", "activeImports", "mergeabilityIssues"):
                    self.assertEqual(report[field], [], field)
                for field in ("changedChecks", "changedMergeability"):
                    self.assertEqual([(item["number"], item["sourceStatus"], item["status"])
                                      for item in report[field]], [(103, status, "open")])
                self.assertEqual(source, retained_source)
                repeated = updater.check_report([source], {"prs": [retained_source]})
                self.assertEqual(repeated["changedChecks"], [])
                self.assertEqual(repeated["changedMergeability"], [])
        terminal = {"prs": [copy.deepcopy(source)]}
        source["status"] = "open"
        reopened = updater.check_report([source], terminal)
        self.assertEqual([item["number"] for item in reopened["checkFailures"]], [103])


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
    @staticmethod
    def check(identity: str, state: str, run: int, *, attempt: int = 1,
              workflow: str = "lint-workflow", app: str = "actions-app") -> dict:
        return {"__typename": "CheckRun", "id": identity, "name": "lint", "status": "COMPLETED",
                "conclusion": state, "detailsUrl": f"https://github.com/test/actions/runs/{run}",
                "checkSuite": {"app": {"id": app, "name": "GitHub Actions", "slug": "github-actions"},
                               "workflowRun": {"runNumber": run, "runAttempt": attempt,
                                               "workflow": {"id": workflow, "name": "Lint"}}}}

    def normalize_checks(self, checks: list[dict], *, complete: bool = True) -> dict:
        pr = raw_pr(15647)
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = {
            "state": "FAILURE", "contexts": {"totalCount": len(checks), "nodes": checks,
                                               "pageInfo": {"hasNextPage": not complete}}}
        return updater.normalize(pr)

    def test_new_attempt_replaces_cancellation_without_changing_raw_rollup(self) -> None:
        old, current = self.check("old", "CANCELLED", 5957), self.check("new", "SUCCESS", 5958)
        result = self.normalize_checks([current, old])
        self.assertEqual([check["id"] for check in result["checks"]["contexts"]], ["new"])
        self.assertEqual([check["id"] for check in result["checks"]["supersededContexts"]], ["old"])
        self.assertEqual((result["checks"]["state"], result["checks"]["total"], result["checks"]["reportedTotal"]),
                         ("FAILURE", 1, 2))
        self.assertEqual(updater.check_states(result), {"SUCCESS"})
        self.assertEqual(updater.check_report([result], {})["checkFailures"], [])
        # A complete inventory explains the obsolete aggregate, but does not
        # change GitHub's merge gate or permit an incomplete list to pass.
        self.assertFalse(checks_passed(result))
        incomplete = self.normalize_checks([old, current], complete=False)
        self.assertIn("FAILURE", updater.check_states(incomplete))

    def test_latest_failure_pending_and_rerun_are_not_hidden(self) -> None:
        for state, expected in [("FAILURE", "FAILURE"), ("IN_PROGRESS", "IN_PROGRESS")]:
            newer = self.check("new", state, 5957, attempt=2)
            if state == "IN_PROGRESS":
                newer.update(status=state, conclusion=None)
            result = self.normalize_checks([self.check("old", "CANCELLED", 5957), newer])
            self.assertEqual(updater.check_states(result), {expected})
            self.assertEqual([check["id"] for check in result["checks"]["contexts"]], ["new"])
            self.assertEqual(bool(updater.check_report([result], {})["checkFailures"]), state == "FAILURE")

    def test_different_workflows_apps_and_same_attempt_jobs_remain_distinct(self) -> None:
        checks = [self.check("old", "CANCELLED", 1),
                  self.check("workflow", "SUCCESS", 2, workflow="other-workflow"),
                  self.check("app", "SUCCESS", 2, app="other-app"),
                  self.check("same-attempt", "SUCCESS", 1)]
        result = self.normalize_checks(checks)
        self.assertEqual([check["id"] for check in result["checks"]["contexts"]],
                         ["old", "workflow", "app", "same-attempt"])
        self.assertEqual(result["checks"]["supersededContexts"], [])
        self.assertIn("CANCELLED", updater.check_states(result))

    def test_missing_provider_identity_does_not_supersede_checks(self) -> None:
        for field in ("app", "workflowRun"):
            old = self.check("old", "CANCELLED", 1)
            old["checkSuite"][field] = None
            result = self.normalize_checks([old, self.check("new", "SUCCESS", 2)])
            self.assertEqual(result["checks"]["supersededContexts"], [])
            self.assertEqual(len(result["checks"]["contexts"]), 2)
        missing_rank = self.check("old", "CANCELLED", 1)
        del missing_rank["checkSuite"]["workflowRun"]["runAttempt"]
        self.assertEqual(self.normalize_checks([missing_rank, self.check("new", "SUCCESS", 2)])
                         ["checks"]["supersededContexts"], [])

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
        self.maintainers = frozenset({"maintainer", "another-maintainer", "nybidari", "aaltinaydev"})
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
        return updater.reminder_plan(retained(self.raw), self.node, now or self.now, "tamird", self.maintainers)

    def test_import_trigger_alarm_replays_three_actual_missing_labels(self) -> None:
        fixture = json.loads((Path(__file__).parent / "import-trigger-fixtures.json").read_text())
        now = datetime.fromisoformat(fixture["cutoff"].replace("Z", "+00:00"))
        for row in fixture["rows"]:
            pr, node = row["pr"], row["node"]
            with self.subTest(number=pr["number"]):
                observed = datetime.fromisoformat(row.get("observedAt", fixture["cutoff"]).replace("Z", "+00:00"))
                plan = updater.reminder_plan(pr, node, observed, "tamird", frozenset({"etienneperot", "lsfriend"}))
                self.assertEqual(plan.get("action"), row["expectedAction"])
                if row["expectedAction"] == "reapprove-amendment":
                    self.assertEqual(plan["status"], "due")
                    self.assertIn("please reapprove the current source head", plan["body"])
                    continue
                if row["expectedAction"] is None:
                    self.assertNotEqual(plan["status"], "due")
                    self.assertEqual(plan["publicImportDisposition"]["state"],
                                     "open" if pr["number"] == 16382 else "conflicting")
                    continue
                self.assertEqual(row["previousPlan"], {"status": "skipped", "reason": "reviewed-patch-preserving-rebase"})
                self.assertEqual((plan["status"], plan["approvers"]), ("due", ["EtiennePerot"]))
                self.assertIn("restore `ready to pull`", plan["body"])
                self.assertNotIn("please reapprove", plan["body"])
                self.assertEqual(plan["publicImportDisposition"]["trigger"]["state"], "missing")
                with mock.patch.dict(pr, {"labels": ["ready to pull"]}):
                    current = updater.reminder_plan(pr, node, now, "tamird", self.maintainers)
                    self.assertEqual(current["reason"], "import-trigger-present-awaiting-replacement")
                    self.assertIsNone(current["publicImportDisposition"]["action"])
                for change in ({"labelsComplete": False}, {"labelHistory": {"complete": False, "items": []}},
                               {"importsComplete": False}, {"mergeable": "CONFLICTING"},
                               {"branchHead": "f" * 40}, {"revisionChanged": True}):
                    with self.subTest(change=change), mock.patch.dict(pr, change):
                        self.assertNotEqual(updater.reminder_plan(pr, node, now, "tamird", self.maintainers)["status"], "due")
                with mock.patch.dict(pr, {"labelHistory": {"complete": True, "items": []}}):
                    self.assertNotEqual(updater.reminder_plan(pr, node, now, "tamird", self.maintainers)["status"], "due")
                comment = {"kind": "comment", "body": updater.reminder_plan(pr, node, now, "tamird", self.maintainers)["body"],
                           "author": {"login": "tamird"}, "createdAt": now.isoformat()}
                pr["feedback"]["items"].append(comment)
                try:
                    for hours, expected in [(2.99, "skipped"), (3, "due")]:
                        result = updater.reminder_plan(pr, node, now + timedelta(hours=hours), "tamird", self.maintainers)
                        self.assertEqual(result["status"], expected)
                    result = updater.reminder_plan(pr, node, datetime(2026, 10, 31, 19, 50, tzinfo=timezone.utc), "tamird", self.maintainers)
                    self.assertEqual(result["reason"], "outside-pacific-business-hours")
                finally:
                    pr["feedback"]["items"].pop()

    def test_actual_open_earlier_exports_require_source_reapproval(self) -> None:
        fixture = json.loads((Path(__file__).parent / "import-trigger-fixtures.json").read_text())
        maintainers = frozenset({"etienneperot", "lsfriend"})
        for row in fixture["openExportRows"]:
            pr, node = row["pr"], row["node"]
            now = datetime.fromisoformat(row["observedAt"])
            with self.subTest(number=pr["number"]):
                plan = updater.reminder_plan(pr, node, now, "tamird", maintainers)
                self.assertEqual(plan.get("action"), row["expectedAction"])
                if row["expectedAction"] is None:
                    self.assertNotEqual(plan["status"], "due")
                    continue
                self.assertEqual((plan["status"], plan["approvers"]), ("due", ["EtiennePerot"]))
                self.assertIn("please reapprove the current source head", plan["body"])
                self.assertIn(pr["head"], plan["body"])
                self.assertNotIn("resolve", plan["body"])
                self.assertNotIn("restore", plan["body"])
                self.assertNotIn("preserve", plan["body"].split("The source rebase")[0])
                if pr["number"] == 15929:
                    self.assertLess(now - datetime.fromisoformat(pr["headIntroducedAt"].replace("Z", "+00:00")), timedelta(hours=3))
                for change in ({"importsComplete": False}, {"mergeable": "CONFLICTING"},
                               {"branchHead": "f" * 40}, {"threads": {"complete": True, "unresolved": 1}}):
                    with mock.patch.dict(pr, change):
                        self.assertNotEqual(updater.reminder_plan(pr, node, now, "tamird", maintainers)["status"], "due")
                successor = {**pr["imports"][0], "number": 99999, "head": "f" * 40,
                             "sourceHead": pr["head"], "matchesSourceHead": True, "mergeable": "MERGEABLE"}
                with mock.patch.dict(pr, {"imports": [*pr["imports"], successor]}):
                    self.assertNotEqual(updater.reminder_plan(pr, node, now, "tamird", maintainers)["status"], "due")
                review = next(item for item in pr["feedback"]["items"] if item["id"] == node["approvedPatch"]["reviewId"])
                fresh = {**review, "id": "fresh", "commit": {"oid": pr["head"]},
                         "submittedAt": now.isoformat(), "updatedAt": now.isoformat(), "body": ""}
                with mock.patch.dict(pr["feedback"], {"items": [*pr["feedback"]["items"], fresh]}):
                    fresh_plan = updater.reminder_plan(pr, node, now, "tamird", maintainers)
                    if pr["number"] == 15940:
                        self.assertEqual(fresh_plan["action"], "restore-import-label")
                    else:
                        self.assertEqual(fresh_plan["reason"], "current-head-approved")
                for reviewer in ("EtiennePerot", "lsfriend"):
                    for labels in ([], ["ready to pull"]):
                        current_review = {**fresh, "author": {**fresh["author"], "login": reviewer}}
                        with mock.patch.dict(pr, {"githubReviewDecision": "REVIEW_REQUIRED", "labels": labels}), \
                                mock.patch.dict(pr["feedback"], {"items": [*pr["feedback"]["items"], current_review]}):
                            current_plan = updater.reminder_plan(pr, node, now, "tamird", maintainers)
                            self.assertTrue(current_plan["publicImportDisposition"]["binding"]["currentApproval"])
                            if pr["number"] == 15940 and not labels:
                                self.assertEqual((current_plan["action"], current_plan["status"], current_plan["approvers"]),
                                                 ("restore-import-label", "due", [reviewer]))
                                self.assertIn("#16324", current_plan["body"])
                                self.assertNotIn("closed", current_plan["body"])
                            else:
                                self.assertEqual(current_plan["reason"], "current-head-approved")
                                self.assertIsNone(current_plan["publicImportDisposition"]["action"])
                for login, kind in (("outsider", "User"), ("tamird", "User"), ("review-bot", "Bot")):
                    unqualified = {**fresh, "author": {"login": login, "__typename": kind}, "authorAssociation": "NONE"}
                    with mock.patch.dict(pr, {"githubReviewDecision": "REVIEW_REQUIRED"}), \
                            mock.patch.dict(pr["feedback"], {"items": [*pr["feedback"]["items"], unqualified]}):
                        unqualified_plan = updater.reminder_plan(pr, node, now, "tamird", maintainers)
                        self.assertEqual(unqualified_plan["status"], "due")
                        self.assertFalse(unqualified_plan["publicImportDisposition"]["binding"]["currentApproval"])
                sent = {"kind": "comment", "author": {"login": "tamird"}, "body": plan["body"],
                        "createdAt": (now - timedelta(hours=2, minutes=59)).isoformat()}
                with mock.patch.dict(pr["feedback"], {"items": [*pr["feedback"]["items"], sent]}):
                    self.assertEqual(updater.reminder_plan(pr, node, now, "tamird", maintainers)["reason"], "reminded-within-three-hours")

    def test_reapproval_after_label_removal_does_not_need_a_patch_comparison(self) -> None:
        fixture = json.loads((Path(__file__).parent / "import-trigger-fixtures.json").read_text())
        row = next(row for row in fixture["openExportRows"] if row["pr"]["number"] == 15940)
        pr, node = row["pr"], row["node"]
        old = next(item for item in pr["feedback"]["items"] if item["id"] == node["approvedPatch"]["reviewId"])
        pr["feedback"]["items"].append({**old, "id": "fresh", "body": "", "commit": {"oid": pr["head"]},
                                        "submittedAt": "2026-10-27T21:50:56Z", "updatedAt": "2026-10-27T21:50:56Z"})
        pr["githubReviewDecision"] = "APPROVED"
        now = datetime(2026, 10, 28, 17, tzinfo=timezone.utc)
        def plan(node):
            return updater.reminder_plan(pr, node, now, "tamird", frozenset({"etienneperot"}))

        self.assertEqual(plan({})["action"], "restore-import-label")
        with mock.patch.dict(pr, {"githubReviewDecision": "REVIEW_REQUIRED"}):
            self.assertEqual(plan({})["action"], "restore-import-label")
        with mock.patch.dict(pr, {"githubReviewDecision": "CHANGES_REQUESTED"}):
            self.assertNotEqual(plan({})["status"], "due")
        for change in ({"importsComplete": False}, {"labelsComplete": False}, {"labels": ["ready to pull"]},
                       {"labelHistory": {"complete": False, "items": []}}):
            with mock.patch.dict(pr, change):
                self.assertNotEqual(plan({})["status"], "due")
        with mock.patch.dict(pr["labelHistory"]["items"][-1], {"createdAt": "2026-10-28T16:00:00Z"}):
            self.assertNotEqual(plan({})["status"], "due")  # Removal came after approval.
        earlier = pr["imports"][0]
        for status in ("open", "draft", "merged"):
            successor = {**earlier, "number": 99999, "sourceHead": pr["head"], "matchesSourceHead": True, "status": status}
            with mock.patch.dict(pr, {"imports": [earlier, successor]}):
                self.assertNotEqual(plan({})["status"], "due")
        with mock.patch.dict(pr, {"imports": [{**earlier, "status": "merged"}, {**successor, "status": "closed"}]}):
            self.assertEqual(plan(node)["publicImportDisposition"]["action"], "maintainer-import-landed")
            self.assertNotEqual(plan(node)["status"], "due")

    def test_label_recovery_respects_current_approval_author_work_and_cross_action_cooldown(self) -> None:
        row = json.loads((Path(__file__).parent / "import-trigger-fixtures.json").read_text())["rows"][0]
        pr, node = row["pr"], row["node"]
        now = datetime(2026, 10, 27, 19, 50, tzinfo=timezone.utc)
        maintainers = frozenset({"etienneperot"})
        approval = {**next(item for item in pr["feedback"]["items"] if item["id"] == node["approvedPatch"]["reviewId"]),
                    "id": "new-approval", "submittedAt": "2026-10-27T19:49:00Z", "updatedAt": "2026-10-27T19:49:00Z",
                    "commit": {"oid": pr["head"]}, "body": ""}
        pr["feedback"]["items"].append(approval)
        pr["githubReviewDecision"] = "APPROVED"
        # Model an exact-head closed export so subsequent author work does not
        # invalidate an older-patch association before reaching the guard.
        pr["imports"][0].update(sourceHead=pr["head"], matchesSourceHead=True)
        self.assertEqual(updater.reminder_plan(pr, node, now, "tamird", maintainers)["action"], "restore-import-label")
        for field, value, reason in [
                ("followup", {"actionableComment": {"head": pr["head"], "commentId": "author-task", "url": pr["url"] + "#issuecomment-1"}}, "registered-current-author-action"),
                ("releaseWait", {"head": pr["head"]}, "registered-current-release-wait")]:
            pr["feedback"]["items"].append({"kind": "comment", "id": "author-task", "url": pr["url"] + "#issuecomment-1",
                "body": "Please fix the source", "author": {"login": "maintainer"}, "createdAt": OLD, "updatedAt": OLD})
            with mock.patch.dict(node, {field: value}):
                self.assertEqual(updater.reminder_plan(pr, node, now, "tamird", maintainers)["reason"], reason)
            pr["feedback"]["items"].pop()
        with mock.patch.dict(pr["threads"], {"unresolved": 1}):
            self.assertEqual(updater.reminder_plan(pr, node, now, "tamird", maintainers)["reason"], "unresolved-or-incomplete-author-threads")
        rebase = next(item for item in pr["feedback"]["items"] if item.get("body") == "[🤖] Needs rebase.")
        with mock.patch.dict(rebase, {"updatedAt": now.isoformat()}):
            self.assertEqual(updater.reminder_plan(pr, node, now, "tamird", maintainers)["reason"],
                             "rebase-request-not-yet-followed-by-source-update")
        pr["feedback"]["items"].append({"kind": "comment", "body": "<!-- codex-reapproval-reminder:v1 head=older -->",
            "author": {"login": "tamird"}, "createdAt": (now - timedelta(hours=2, minutes=59)).isoformat()})
        self.assertEqual(updater.reminder_plan(pr, node, now, "tamird", maintainers)["reason"], "reminded-within-three-hours")

    def test_amendment_probe_cannot_post_new_label_recovery_with_stale_exports(self) -> None:
        self.raw["reviewDecision"] = "APPROVED"
        self.raw["reviews"]["nodes"] = [{**self.review, "commit": {"oid": f"{8:040x}"}, "submittedAt": LATER}]
        self.raw["labelHistory"]["nodes"] = [
            {"__typename": kind, "id": kind, "createdAt": stamp, "actor": {"login": "maintainer"},
             "label": {"name": "ready to pull"}}
            for kind, stamp in [("LabeledEvent", OLD), ("UnlabeledEvent", LATER)]]
        source = retained(self.raw, [linked(imported(108, 8, "CLOSED"), 8)])
        source["labels"] = ["ready to pull"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "github-status.json").write_text(json.dumps({"checkedAt": self.now.isoformat(), "prs": [source]}))
            registry = {"meta": {"repo": "google/gvisor", "owner": "tamird"}, "nodes": [self.node]}
            with mock.patch.multiple(updater, ROOT=root, REGISTRY=registry), \
                    mock.patch.object(updater, "fetch_maintainers", return_value=self.maintainers), \
                    mock.patch.object(updater, "fetch_numbers", return_value={7: self.raw}) as fetch, \
                    mock.patch.object(updater.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "tamird\n", "")) as calls, \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                updater.reapproval_reminders(post=True)
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["reason"],
                             "import-trigger-changed-needs-fresh-lineage")
            fetch.assert_called_once_with([7], timeline=False, repo="google/gvisor", team_details=False)
            self.assertEqual(calls.call_args.args[0], ["gh", "api", "user", "--jq", ".login"])
            self.assertEqual(calls.call_count, 1)

    def test_reminder_export_refresh_discovers_exact_successor(self) -> None:
        old = linked(imported(107, 7, "CLOSED"), 7)
        source = retained(self.raw, [old])
        successor = imported(108, 8)
        successor["body"] = successor["body"].replace("/pull/8 from tamird:topic-8", "/pull/7 from tamird:topic-7")
        self.raw["timelineItems"]["nodes"] = [{"source": successor}]
        with mock.patch.object(updater, "fetch_numbers", return_value={107: imported(107, 7, "CLOSED"), 108: successor}) as fetch:
            result = updater.refresh_reminder_imports(self.raw, source)
        self.assertEqual([(item["number"], item["matchesSourceHead"]) for item in result], [(107, False), (108, True)])
        fetch.assert_called_once_with([107, 108], timeline=False, repo="google/gvisor", team_details=False)

    def test_canonical_roster_supplements_repository_association(self) -> None:
        for login, association in [("nybidari", "CONTRIBUTOR"), ("aaltinaydev", "NONE")]:
            with self.subTest(login=login):
                self.review["author"] = {"login": login, "__typename": "User"}
                self.review["authorAssociation"] = association
                self.assertEqual(self.plan()["approvers"], [login])
        self.review.update(state="APPROVED", submittedAt=LATER, commit={"oid": f"{8:040x}"})
        self.assertEqual(self.plan()["reason"], "current-head-approved")
        self.review["author"] = {"login": "outside-roster", "__typename": "User"}
        for association in ["CONTRIBUTOR", "NONE"]:
            self.review["authorAssociation"] = association
            self.assertEqual(self.plan()["reason"], "no-prior-approval")
        for association in ["MEMBER", "OWNER", "COLLABORATOR"]:
            self.review["authorAssociation"] = association
            self.assertEqual(self.plan()["reason"], "current-head-approved")

    def test_generated_roster_excludes_emeritus_and_rejects_malformed_rows(self) -> None:
        text = ("<!-- Generated from governance/maintainers.yaml by\n"
                "     //governance/tools/maintainers:maintainers_gen; do not edit directly. -->\n\n"
                "Name | GitHub ID | Company/Organization\n"
                "---- | --------- | --------------------\n"
                "Nayana Bidari | [@nybidari](https://github.com/nybidari) | Google\n"
                "Etienne Perot | [@EtiennePerot](https://github.com/etienneperot) | Google\n"
                "\n## Emeritus Maintainers\n"
                "Adin Scannell | [@amscanne](https://github.com/amscanne) | 2023-03-03\n")
        self.assertEqual(updater.maintainer_logins(text), frozenset({"nybidari", "etienneperot"}))
        for malformed in (text.replace("https://github.com/nybidari", "https://github.com/outsider"),
                          text.replace("## Emeritus Maintainers", "## Former maintainers"),
                          text.replace("Nayana Bidari |", "Nayana Bidari ")):
            with self.subTest(text=malformed), self.assertRaises(ValueError):
                updater.maintainer_logins(malformed)
        with mock.patch.object(updater, "graphql", return_value={"repository": {"object": {"oid": "a" * 40, "text": text}}}):
            self.assertEqual(updater.fetch_maintainers(), frozenset({"nybidari", "etienneperot"}))

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
            self.assertEqual(self.plan()["status"], "needs-evidence")
        for author, association in [({"login": "tamird", "__typename": "User"}, "OWNER"),
                                    ({"login": "bot", "__typename": "Bot"}, "MEMBER"),
                                    ({"login": "outsider", "__typename": "User"}, "NONE")]:
            with self.subTest(author=author), mock.patch.dict(self.review, {"author": author, "authorAssociation": association}):
                self.assertNotEqual(self.plan()["status"], "due")

    def test_earlier_export_needs_current_source_reapproval_but_landed_patch_does_not(self) -> None:
        old, head = f"{7:040x}", f"{8:040x}"
        patch = {"method": "stable-patch-id", "head": head, "approvedHead": old,
                 "reviewId": self.review["id"], "reviewer": "maintainer", "submittedAt": OLD,
                 "verifiedAt": LATER, "oldPatchId": "a" * 40, "newPatchId": "a" * 40}
        export = linked(imported(107, 7), 7)
        pr = retained(self.raw, [export])
        pr.update(headIntroducedAt=LATER, headCommittedAt=LATER)
        self.node["approvedPatch"] = patch
        plan = updater.reminder_plan(pr, self.node, self.now, "tamird", self.maintainers)
        self.assertEqual((plan["status"], plan["action"]), ("due", "reapprove-amendment"))
        self.assertIn(export["url"], plan["body"])
        export["mergeable"] = "CONFLICTING"
        plan = updater.reminder_plan(pr, self.node, self.now, "tamird", self.maintainers)
        self.assertEqual(plan["status"], "due")
        self.assertEqual(plan["publicImportDisposition"]["action"], "maintainer-reapproval")
        export["status"] = "closed"
        plan = updater.reminder_plan(pr, self.node, self.now, "tamird", self.maintainers)
        self.assertEqual(plan["reason"], "reviewed-patch-preserving-rebase")
        self.assertEqual(plan["publicImportDisposition"]["action"], "maintainer-reimport")
        replacement = linked(imported(108, 8), 8)
        pr["imports"].append(replacement)
        disposition = updater.reminder_plan(pr, self.node, self.now, "tamird", self.maintainers)["publicImportDisposition"]
        self.assertEqual((disposition["scope"], disposition["state"], disposition["action"]), ("exact", "open", None))
        self.assertEqual([item["number"] for item in disposition["binding"]["imports"]], [108])
        pr["imports"].pop()
        export["status"] = "open"
        export["sourceHead"] = "f" * 40
        self.assertEqual(updater.reminder_plan(pr, self.node, self.now, "tamird", self.maintainers)["reason"],
                         "reviewed-patch-preserving-rebase")
        landing = {"method": "merge-tree-identical", "head": head, "upstreamHead": "d" * 40,
                   "upstreamTree": "e" * 40, "replayedTree": "e" * 40, "verifiedAt": LATER,
                   "evidence": [export["url"]]}
        self.node["landedPatch"] = landing
        self.assertEqual(self.plan()["reason"], "patch-already-on-master")
        landing["head"] = old
        self.assertEqual(self.plan()["reason"], "reviewed-patch-preserving-rebase")

    def test_current_approval_changes_requested_draft_and_closed_stop_reminders(self) -> None:
        for state, reason in [("APPROVED", "current-head-approved"),
                              ("CHANGES_REQUESTED", "current-head-needs-source-work")]:
            current = {**self.review, "id": "new-review", "state": state, "submittedAt": LATER,
                       "author": {"login": "another-maintainer", "__typename": "User"},
                       "commit": {"oid": f"{8:040x}"}}
            with mock.patch.dict(self.raw["reviews"], {"nodes": [self.review, current]}):
                self.assertEqual(self.plan()["reason"], reason)
                current["author"] = {"login": "outsider", "__typename": "User"}
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

    def test_conflicts_and_unknown_mergeability_defer_reminders(self) -> None:
        for value, reason in [("CONFLICTING", "source-conflicted"), ("UNKNOWN", "mergeability-unknown"),
                              (None, "mergeability-unknown")]:
            with self.subTest(mergeable=value), mock.patch.dict(self.raw, {"mergeable": value}):
                self.assertEqual((self.plan()["status"], self.plan()["reason"]), ("deferred", reason))
        self.raw["headRef"] = {"name": self.raw["headRefName"], "target": {"oid": "b" * 40}}
        self.assertEqual(self.plan()["reason"], "source-branch-differs-from-pr")
        self.raw["headRef"]["target"]["oid"] = self.raw["headRefOid"]
        self.assertEqual(self.plan()["status"], "due")

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

    def test_comment_history_deduplicates_three_hours_including_head_changes(self) -> None:
        self.now = datetime(2026, 10, 22, 20, 30, tzinfo=timezone.utc)
        comment = {"kind": "comment", "author": {"login": "tamird", "__typename": "User"},
                   "createdAt": "2026-10-22T17:31:00Z", "body": self.plan()["body"]}
        self.raw["comments"]["nodes"] = [comment]
        for stamp in ("2026-10-22T17:31:00Z", "2026-10-22T17:30:01Z"):
            with self.subTest(stamp=stamp):
                comment["createdAt"] = stamp
                self.assertEqual(self.plan()["reason"], "reminded-within-three-hours")
        comment["body"] = comment["body"].replace(f"head={8:040x}", "head=" + "e" * 40)
        self.assertEqual(self.plan()["reason"], "reminded-within-three-hours")
        comment["createdAt"] = "2026-10-22T17:30:00Z"
        self.assertEqual(self.plan()["status"], "due")
        self.raw["comments"]["pageInfo"]["hasPreviousPage"] = True
        self.assertEqual(self.plan()["status"], "due")  # History covers the full three-hour window.
        comment["createdAt"] = "2026-10-22T17:31:00Z"
        self.assertEqual(self.plan()["reason"], "incomplete-reminder-window")

    def test_reminder_query_omits_only_scope_restricted_team_details(self) -> None:
        with mock.patch.object(updater, "graphql", return_value={"repository": {"p7": self.raw}}) as query:
            updater.fetch_numbers([7], timeline=False)
            full = query.call_args.args[0]
            updater.fetch_numbers([7], timeline=False, team_details=False)
            public = query.call_args.args[0]
        self.assertIn("... on Team { name slug }", full)
        self.assertNotIn("... on Team", public)
        self.assertEqual(public, full.replace(" ... on Team { name slug }", ""))

    def test_post_uses_fresh_state_and_never_retries_an_ambiguous_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "github-status.json").write_text(json.dumps({"checkedAt": self.now.isoformat(), "prs": [retained(self.raw)]}))
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
                    mock.patch.object(updater, "fetch_maintainers", return_value=self.maintainers), \
                    mock.patch.object(updater, "datetime") as clock, \
                    mock.patch.object(updater, "fetch_numbers", return_value={7: self.raw}) as fetch, \
                    mock.patch.object(updater.subprocess, "run", side_effect=run) as transport:
                clock.now.return_value = self.now
                updater.reapproval_reminders(post=True)
                self.assertEqual(fetch.call_args_list, [
                    mock.call([7], timeline=False, repo="google/gvisor", team_details=False),
                    mock.call([7], timeline=False, repo="google/gvisor", team_details=False)])
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
                self.raw["mergeable"] = "CONFLICTING"
                updater.reapproval_reminders(post=True)
                self.assertEqual(len(comments), 1)
                self.raw["mergeable"] = "UNKNOWN"
                unresolved = {key: self.raw[key] for key in
                              ("number", "headRefOid", "baseRefOid", "state", "mergeable", "mergeStateStatus")}
                with mock.patch.object(updater, "requests", 0), \
                        mock.patch.object(updater, "graphql", return_value={"repository": {"p7": unresolved}}) as query:
                    updater.reapproval_reminders(post=True)
                    query.assert_called_once()
                    self.assertEqual(len(comments), 1)
                    unresolved["headRefOid"] = "a" * 40
                    with self.assertRaisesRegex(RuntimeError, "changed during mergeability recheck"):
                        updater.reapproval_reminders(post=True)
                    self.assertEqual(len(comments), 1)
                    unresolved.update(headRefOid=f"{8:040x}", mergeable="MERGEABLE")
                    updater.reapproval_reminders(post=True)
                    self.assertEqual(len(comments), 2)
                transport.side_effect = [subprocess.CompletedProcess([], 0, "tamird\n", ""),
                                         subprocess.TimeoutExpired("gh", 60)]
                calls = transport.call_count
                with self.assertRaises(subprocess.TimeoutExpired):
                    updater.reapproval_reminders(post=True)
                self.assertEqual(transport.call_count - calls, 2)
                transport.side_effect = [subprocess.CompletedProcess([], 0, "tamird\n", ""),
                                         subprocess.CalledProcessError(1, "gh", stderr="permission denied (HTTP 403)")]
                calls = transport.call_count
                with self.assertRaisesRegex(RuntimeError, r"permission denied \(HTTP 403\); not retried"):
                    updater.reapproval_reminders(post=True)
                self.assertEqual(transport.call_count - calls, 2)

    def test_lightweight_reminder_probe_keeps_only_same_head_import_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = retained(self.raw, [linked(imported(108, 8), 8)])
            snapshot = {"checkedAt": self.now.isoformat(), "prs": [source]}
            (root / "github-status.json").write_text(json.dumps(snapshot))
            registry = {"meta": {"repo": "google/gvisor", "owner": "tamird"}, "nodes": [self.node]}
            with mock.patch.multiple(updater, ROOT=root, REGISTRY=registry), \
                    mock.patch.object(updater, "fetch_maintainers", return_value=self.maintainers), \
                    mock.patch.object(updater, "fetch_numbers", return_value={7: self.raw}), \
                    mock.patch.object(updater, "reminder_plan", wraps=updater.reminder_plan) as planner, \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                updater.reapproval_reminders()
                current = planner.call_args.args[0]
                self.assertEqual(current["imports"], source["imports"])
                self.assertEqual(current["importsComplete"], source["importsComplete"])
                result = json.loads(output.getvalue().splitlines()[-1])
                self.assertEqual(result["importSnapshotAt"], snapshot["checkedAt"])
                self.raw["headRefOid"] = "a" * 40
                self.raw["commits"]["nodes"][0]["commit"]["oid"] = "a" * 40
                updater.reapproval_reminders()
                self.assertFalse(planner.call_args.args[0].get("importsComplete"))
                result = json.loads(output.getvalue().splitlines()[-1])
                self.assertIsNone(result["importSnapshotAt"])


if __name__ == "__main__":
    unittest.main()
