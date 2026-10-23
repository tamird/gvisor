#!/usr/bin/env python3
"""Publish public GitHub metadata using the existing authenticated gh session."""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from reapproval import reminder_plan
from work_states import FAILURES, check_purpose, check_states, effective_approval, record_states, remember_mergeability, review_decision, repository_policies


ROOT = Path(__file__).resolve().parent
REGISTRY = json.loads((ROOT / "registry.json").read_text())
REPO = REGISTRY["meta"]["repo"]
OWNER = REGISTRY["meta"]["owner"]
REQUEST_LIMIT = 48
PR_BATCH_SIZE = 8
requests = 0
query_cost = 0
rate_remaining = None

CHECKS = """
commits(last:1) { nodes { commit { oid committedDate statusCheckRollup {
  state contexts(first:100) { totalCount pageInfo { hasNextPage }
    nodes { __typename
      ... on CheckRun { id name status conclusion detailsUrl checkSuite {
        app { id name slug } workflowRun { runNumber runAttempt workflow { id name } }
      } }
      ... on StatusContext { id context state targetUrl description }
    }
  }
} } } }
"""
FIELDS = """
number title url state isDraft headRefName headRefOid headRef { name target { oid } } baseRefOid updatedAt createdAt mergedAt closedAt mergeCommit { oid }
headRepository { nameWithOwner owner { login } } repository { nameWithOwner }
author { login } reviewDecision mergeable mergeStateStatus
headHistory:timelineItems(last:1,itemTypes:[HEAD_REF_FORCE_PUSHED_EVENT]) {
  nodes { ... on HeadRefForcePushedEvent { createdAt afterCommit { oid } } }
}
labels(first:100) { nodes { name } pageInfo { hasNextPage } }
comments(last:100) { pageInfo { hasPreviousPage } nodes {
  id url body createdAt updatedAt author { login __typename }
} }
reviews(last:100) { pageInfo { hasPreviousPage } nodes {
  id url body state submittedAt updatedAt authorAssociation author { login __typename } commit { oid }
} }
reviewDismissals:timelineItems(last:100,itemTypes:[REVIEW_DISMISSED_EVENT]) {
  pageInfo { hasPreviousPage }
  nodes { ... on ReviewDismissedEvent { id createdAt previousReviewState review { id } } }
}
reviewRequests(first:100) { pageInfo { hasNextPage } nodes { requestedReviewer {
  __typename ... on User { login } ... on Team { name slug }
} } }
reviewThreads(first:100) { totalCount pageInfo { hasNextPage } nodes {
  id isResolved isOutdated path line
  comments(last:100) { pageInfo { hasPreviousPage } nodes {
    id url body createdAt updatedAt author { login __typename }
  } }
} }
""" + CHECKS
TIMELINE = """
timelineItems(last:100,itemTypes:[CROSS_REFERENCED_EVENT]) {
  pageInfo { hasPreviousPage }
  nodes { ... on CrossReferencedEvent { id createdAt source { __typename
    ... on PullRequest { number url title body updatedAt headRefOid author { login } repository { nameWithOwner isPrivate } }
  } } }
}
"""


class GitHubTransportError(RuntimeError):
    """A bounded request failed before a usable GitHub response arrived."""


def graphql(query: str, variables: dict[str, object] | None = None) -> dict:
    global requests, query_cost, rate_remaining
    query = query.rstrip().removesuffix("}") + " rateLimit { cost remaining } }"
    for attempt in range(2):
        requests += 1
        if requests > REQUEST_LIMIT:
            raise RuntimeError("Snapshot request limit reached; previous file is unchanged")
        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "--input", "-"],
                input=json.dumps({"query": query, "variables": variables or {}}),
                text=True, capture_output=True, check=False, timeout=60,
            )
        except subprocess.TimeoutExpired:
            if attempt == 0 and requests < REQUEST_LIMIT:
                print("GitHub query timed out; retrying once within the request cap", file=sys.stderr)
                continue
            raise GitHubTransportError("GitHub query timed out") from None
        if result.returncode:
            # Retry this read-only query once for transport or gateway errors.
            interrupted = any(error in result.stderr for error in (
                "net/http: TLS handshake timeout", "unexpected EOF", "gh: HTTP 499",
                "i/o timeout", "connection reset by peer", "gh: HTTP 502", "gh: HTTP 504",
            ))
            if attempt == 0 and requests < REQUEST_LIMIT and interrupted:
                print("GitHub transport interrupted; retrying once within the request cap", file=sys.stderr)
                continue
            if interrupted:
                raise GitHubTransportError(f"GitHub transport failed: {result.stderr.strip()}")
            raise RuntimeError(f"GitHub query failed: {result.stderr.strip() or 'gh returned no diagnostic'}")
        response = json.loads(result.stdout)
        if response.get("errors"):
            raise RuntimeError(f"GitHub query failed: {response['errors']}")
        data = response["data"]
        rate = data.pop("rateLimit")
        query_cost += rate["cost"]
        rate_remaining = rate["remaining"]
        return data


def public_url(value: str | None) -> str | None:
    # Internal Copybara CL links are not public check-log links.
    return value if value and value.startswith("https://") else None


def feedback(pr: dict) -> dict:
    items = []
    complete = not pr["reviewThreads"]["pageInfo"]["hasNextPage"]
    for kind, key in (("comment", "comments"), ("review", "reviews")):
        connection = pr[key]
        complete &= not connection["pageInfo"]["hasPreviousPage"]
        items.extend({"kind": kind, **item} for item in connection["nodes"])
    for thread in pr["reviewThreads"]["nodes"]:
        complete &= not thread["comments"]["pageInfo"]["hasPreviousPage"]
        items.extend({"kind": "inline", "threadId": thread["id"],
                      "isResolved": thread["isResolved"], "isOutdated": thread["isOutdated"],
                      "path": thread["path"], "line": thread["line"], **item}
                     for item in thread["comments"]["nodes"])
    return {"complete": complete, "items": items}


def changed_feedback(current: dict, previous: dict) -> list[dict]:
    # checkedAt records an attribute observation, not acknowledgment of feedback.
    # Missing prior feedback therefore requires a backfill, regardless of age,
    # approval state, or whether an inline thread is resolved or outdated.
    prior = {item["id"]: item for item in previous.get("feedback", {}).get("items", [])}
    return [item for item in current["feedback"]["items"]
            if (item.get("author") or {}).get("login") != OWNER and prior.get(item["id"]) != item]


def changed_references(current: dict, previous: dict) -> list[dict]:
    # References are evidence to inspect, not inferred dependencies or imports.
    prior = {item["id"]: item for item in previous.get("crossReferences", {}).get("items", [])}
    return [item for item in current.get("crossReferences", {}).get("items", [])
            if prior.get(item["id"]) != item]


def review_watch_report(prs: list[dict], previous: dict) -> list[dict]:
    """Route explicit review subscriptions using the existing snapshot cursor."""
    watches = {(node["repo"], node["number"]): node["reviewWatch"]
               for node in REGISTRY["nodes"] if node.get("reviewWatch")}
    prior = {(pr.get("repo", REPO), pr["number"]): pr for pr in previous.get("prs", [])}
    events = []
    for pr in prs:
        key = pr.get("repo", REPO), pr["number"]
        if not (watch := watches.get(key)):
            continue
        old = prior.get(key, {})
        old_items = {item["id"]: item for item in old.get("feedback", {}).get("items", [])}
        feedback = [item for item in changed_feedback(pr, old)
                    if item.get("body", "").strip()
                    and any(item.get(field) != old_items.get(item["id"], {}).get(field)
                            for field in ("body", "state", "updatedAt"))]
        if pr["head"] == old.get("head") and pr["status"] == old.get("status") and not feedback:
            continue
        events.append({"repo": key[0], "number": key[1], "url": pr["url"], "head": pr["head"],
                       "previousHead": old.get("head"), "status": pr["status"],
                       "owner": watch["owner"], "reviewers": watch["reviewers"],
                       "feedbackComplete": pr["feedback"]["complete"],
                       "feedback": [{field: item.get(field) for field in ("id", "kind", "url", "updatedAt")}
                                    for item in feedback]})
    return events


def current_check_attempts(contexts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate older attempts only when the provider identifies the workflow."""
    latest: dict[tuple[str, str, str], tuple[int, int]] = {}
    identities = []
    for check in contexts:
        key = check.get("appId"), check.get("workflowId"), check["name"]
        rank = check.get("runNumber"), check.get("runAttempt")
        identified = (check.get("kind") == "check-run" and all(isinstance(value, str) and value for value in key)
                      and all(type(value) is int and value > 0 for value in rank))
        identity = (key, rank) if identified else None
        identities.append(identity)
        if identity is not None:
            latest[key] = max(latest.get(key, rank), rank)
    current, superseded = [], []
    for check, identity in zip(contexts, identities):
        # Retain every job at the newest rank, including equal-name matrix jobs.
        destination = superseded if identity is not None and identity[1] < latest[identity[0]] else current
        destination.append(check)
    return current, superseded


def normalize(pr: dict) -> dict:
    commits = pr["commits"]["nodes"]
    if len(commits) != 1 or commits[0]["commit"]["oid"] != pr["headRefOid"]:
        raise RuntimeError(f"PR #{pr['number']} head changed during retrieval")
    branch = pr.get("headRef")
    if branch and (branch["name"] != pr["headRefName"]
                   or not re.fullmatch(r"[a-f0-9]{40}", branch["target"]["oid"])):
        raise RuntimeError(f"PR #{pr['number']} has an invalid source branch identity")
    rollup = commits[0]["commit"]["statusCheckRollup"]
    checks = None
    if rollup is not None:
        connection = rollup["contexts"]
        contexts = []
        for item in connection["nodes"]:
            if item["__typename"] == "CheckRun":
                state = item["conclusion"] if item["status"] == "COMPLETED" else item["status"]
                suite = item["checkSuite"]
                app = suite.get("app") or {}
                run = suite.get("workflowRun") or {}
                workflow = run.get("workflow") or {}
                # This repository-owned workflow requests reviewers; it does not
                # validate contributor source. Keep its raw outcome observable.
                context = {"id": item["id"], "name": item["name"], "state": state or "UNKNOWN",
                                 "url": public_url(item["detailsUrl"]), "kind": "check-run",
                                 "app": app.get("name"), "appSlug": app.get("slug"), "appId": app.get("id"),
                                 "workflow": workflow.get("name"), "workflowId": workflow.get("id"),
                                 "runNumber": run.get("runNumber"), "runAttempt": run.get("runAttempt")}
                context["purpose"] = check_purpose(context)
                contexts.append(context)
            else:
                contexts.append({"id": item["id"], "name": item["context"], "state": item["state"],
                                 "url": public_url(item["targetUrl"]), "description": item.get("description"),
                                 "app": None, "kind": "status",
                                 "purpose": "validation"})
        current, superseded = current_check_attempts(contexts)
        checks = {"state": rollup["state"], "total": connection["totalCount"] - len(superseded),
                  "reportedTotal": connection["totalCount"], "supersededContexts": superseded,
                  "complete": not connection["pageInfo"]["hasNextPage"], "contexts": current}
    threads = pr["reviewThreads"]
    normalized = {
        "number": pr["number"], "repo": pr["repository"]["nameWithOwner"], "title": pr["title"], "url": pr["url"],
        "status": "draft" if pr["isDraft"] and pr["state"] == "OPEN" else pr["state"].lower(),
        "ref": pr["headRefName"], "head": pr["headRefOid"], "base": pr["baseRefOid"],
        "branchHead": branch["target"]["oid"] if branch else None,
        "headCommittedAt": commits[0]["commit"]["committedDate"],
        "headHistoryComplete": "headHistory" in pr,
        "headIntroducedAt": max((event["createdAt"] for event in pr.get("headHistory", {}).get("nodes", [])
                                 if (event.get("afterCommit") or {}).get("oid") == pr["headRefOid"]), default=None),
        "headOwner": (pr["headRepository"] or {}).get("owner", {}).get("login"),
        "headRepo": (pr["headRepository"] or {}).get("nameWithOwner"),
        "authorLogin": (pr.get("author") or {}).get("login"),
        "updatedAt": pr["updatedAt"], "createdAt": pr["createdAt"],
        "mergedAt": pr["mergedAt"], "closedAt": pr["closedAt"],
        "mergeCommit": (pr.get("mergeCommit") or {}).get("oid"),
        "githubReviewDecision": pr["reviewDecision"],
        "reviewsComplete": not pr["reviews"]["pageInfo"]["hasPreviousPage"],
        "commentsComplete": not pr["comments"]["pageInfo"]["hasPreviousPage"],
        "reviewDismissals": {"complete": not pr.get("reviewDismissals", {}).get("pageInfo", {}).get("hasPreviousPage", True),
                             "items": pr.get("reviewDismissals", {}).get("nodes", [])},
        "reviewRequests": [item["requestedReviewer"] for item in pr["reviewRequests"]["nodes"] if item["requestedReviewer"]],
        "reviewRequestsComplete": not pr["reviewRequests"]["pageInfo"]["hasNextPage"],
        "mergeable": pr["mergeable"],
        "mergeabilityCheckedAt": pr.get("_mergeabilityRetrievedAt", pr["_retrievedAt"]),
        "mergeState": pr["mergeStateStatus"],
        "labels": [label["name"] for label in pr["labels"]["nodes"]],
        "labelsComplete": not pr["labels"]["pageInfo"]["hasNextPage"],
        "threads": {"unresolved": sum(not thread["isResolved"] for thread in threads["nodes"]),
                    "total": threads["totalCount"], "complete": not threads["pageInfo"]["hasNextPage"]},
        "feedback": feedback(pr),
        "checks": checks,
    }

    if timeline := pr.get("timelineItems"):
        normalized["crossReferences"] = {
            "complete": not timeline["pageInfo"]["hasPreviousPage"],
            "items": [event for event in timeline["nodes"]
                      if (source := event.get("source") or {}).get("__typename") == "PullRequest"
                      and source.get("repository", {}).get("isPrivate") is False],
        }
    normalized["reviewDecision"] = review_decision(normalized)
    normalized["approvedHead"] = normalized["head"] if normalized["reviewDecision"] == "APPROVED" else None
    approval = effective_approval(normalized)
    normalized["approvedAt"] = approval["submittedAt"] if approval else None
    return normalized


def fetch_numbers(numbers: list[int], timeline: bool, repo: str = REPO, *, team_details: bool = True) -> dict[int, dict]:
    result = {}
    owner, name = repo.split("/")
    # Reminder decisions need individual reviews, not organization team names.
    selected_fields = FIELDS if team_details else FIELDS.replace(" ... on Team { name slug }", "")
    for start in range(0, len(numbers), PR_BATCH_SIZE):
        fields = " ".join(f"p{number}:pullRequest(number:{number}){{{selected_fields}{TIMELINE if timeline else 'body'}}}"
                          for number in numbers[start:start + PR_BATCH_SIZE])
        response = graphql(f'query {{ repository(owner:"{owner}",name:"{name}") {{ {fields} }} }}')
        for pr in response["repository"].values():
            if pr is None:
                raise RuntimeError("A tracked public PR was unavailable; previous snapshot is unchanged")
            pr["_retrievedAt"] = datetime.now(timezone.utc).isoformat()
            result[pr["number"]] = pr
    return result


def import_source(candidate: dict, source_url: str, head_owner: str | None, head_ref: str, repo: str = REPO) -> str | None:
    if candidate.get("repository", {}).get("nameWithOwner") != repo or (candidate.get("author") or {}).get("login") != "copybara-service":
        return None
    footer = re.compile(r"FUTURE_COPYBARA_INTEGRATE_REVIEW=" + re.escape(source_url)
                        + " from " + re.escape(f"{head_owner}:{head_ref}")
                        + r" ([0-9a-f]{40})")
    heads = {match[1] for line in candidate["body"].splitlines() if (match := footer.fullmatch(line.strip()))}
    if len(heads) > 1:
        raise RuntimeError(f"Import #{candidate['number']} contains ambiguous source revisions")
    return next(iter(heads), None)


def open_inventory(repo: str = REPO) -> dict[int, str | None]:
    """Discover new author PRs and reopened tracked sources/imports together."""
    owner, name = repo.split("/")
    cursor = None
    numbers = {}
    total = None
    while True:
        query = """query($owner:String!,$name:String!,$after:String) {
          repository(owner:$owner,name:$name) {
            pullRequests(states:OPEN,first:100,after:$after) {
              totalCount pageInfo { hasNextPage endCursor }
              nodes { number author { login } }
            }
          }
        }"""
        found = graphql(query, {"owner": owner, "name": name, "after": cursor})["repository"]["pullRequests"]
        if total is not None and total != found["totalCount"]:
            raise RuntimeError("Open PR inventory changed during retrieval")
        total = found["totalCount"]
        for item in found["nodes"]:
            if item["number"] in numbers:
                raise RuntimeError("Duplicate open PR in paginated inventory")
            numbers[item["number"]] = (item["author"] or {}).get("login")
        if not found["pageInfo"]["hasNextPage"]:
            break
        next_cursor = found["pageInfo"]["endCursor"]
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError("Open PR inventory did not advance")
        cursor = next_cursor
    if len(numbers) != total:
        raise RuntimeError("Open PR inventory is incomplete")
    if sum(author == OWNER for author in numbers.values()) > 200:
        raise RuntimeError("Author's open PR inventory exceeds the documented 200-PR bound")
    return numbers


def needs_refresh(number: int, previous: dict[int, dict], open_numbers: dict[int, str | None]) -> bool:
    """Terminal history keeps its observation time until its PR reopens."""
    return number in open_numbers or previous.get(number, {}).get("status") not in {"closed", "merged"}


def recheck_unknown_mergeability(prs: list[dict], repo: str = REPO) -> dict[str, object]:
    """Give GitHub's asynchronous calculation one bounded, head-checked read."""
    unknown = {pr["number"]: pr for pr in prs
               if pr["state"] == "OPEN" and pr["mergeable"] == "UNKNOWN"}
    if not unknown:
        return {"status": "not-needed", "numbers": []}
    if requests >= REQUEST_LIMIT:
        return {"status": "budget-exhausted", "numbers": sorted(unknown)}
    owner, name = repo.split("/")
    fields = " ".join(f"p{number}:pullRequest(number:{number}){{number headRefOid headRef{{name target{{oid}}}} baseRefOid state mergeable mergeStateStatus}}"
                      for number in unknown)
    try:
        response = graphql(f'query {{ repository(owner:"{owner}",name:"{name}") {{ {fields} }} }}')["repository"]
    except GitHubTransportError as error:
        # The primary observations remain valid UNKNOWN results. This optional
        # read must not discard them or advance their mergeability timestamps.
        print(f"Mergeability recheck unavailable; retaining primary observations: {error}", file=sys.stderr)
        return {"status": "unavailable", "numbers": sorted(unknown)}
    retrieved_at = datetime.now(timezone.utc).isoformat()
    if set(response) != {f"p{number}" for number in unknown}:
        raise RuntimeError("Mergeability recheck inventory is incomplete; previous snapshot is unchanged")
    for number, pr in unknown.items():
        result = response[f"p{number}"]
        if (not result or result["number"] != number or result["headRefOid"] != pr["headRefOid"]
                or result["state"] != pr["state"]):
            raise RuntimeError(f"PR #{number} changed during mergeability recheck; previous snapshot is unchanged")
        if result["mergeable"] not in {"MERGEABLE", "CONFLICTING", "UNKNOWN"}:
            raise RuntimeError(f"Unexpected mergeability for PR #{number}; previous snapshot is unchanged")
        pr.update(baseRefOid=result["baseRefOid"], mergeable=result["mergeable"],
                  headRef=result.get("headRef"),
                  mergeStateStatus=result["mergeStateStatus"], _mergeabilityRetrievedAt=retrieved_at)
    return {"status": "completed", "numbers": sorted(unknown)}


def collect_repository(repo: str, policy: str, previous: dict) -> tuple[list[dict], dict, dict, dict]:
    numbers = {node["number"] for node in REGISTRY["nodes"]
               if node["type"] == "pr" and node["repo"] == repo}
    prior_sources = {pr["number"]: pr for pr in previous.get("prs", []) if pr.get("repo", REPO) == repo}
    prior_imports = {pr["number"]: pr for source in prior_sources.values() for pr in source.get("imports", [])}
    numbers.update(prior_sources)
    open_numbers = open_inventory(repo)
    numbers.update(number for number, author in open_numbers.items() if author == OWNER)
    originals = fetch_numbers(sorted(number for number in numbers
                                     if needs_refresh(number, prior_sources, open_numbers)), timeline=True, repo=repo)
    relations = {number: {pr["number"]: pr["sourceHead"]
                          for pr in prior_sources.get(number, {}).get("imports", [])}
                 for number in numbers}
    timeline_relations = {}
    changed_relations = set()
    for number, pr in originals.items():
        matches = {}
        for event in pr.get("timelineItems", {}).get("nodes", []):
            if policy != "copybara":
                continue
            candidate = event.get("source") or {}
            head_owner = (pr["headRepository"] or {}).get("owner", {}).get("login")
            if head := import_source(candidate, pr["url"], head_owner, pr["headRefName"], repo):
                matches[candidate["number"]] = head
        timeline_relations[number] = matches
        changed_relations.update(other for other, head in matches.items() if relations[number].get(other) != head)
        relations[number].update(matches)
    # Historical associations stay visible. An active or reopened import still
    # gets a full observation even when its original source PR is terminal.
    import_numbers = {other for matches in relations.values() for other in matches}
    imported = fetch_numbers(sorted(other for other in import_numbers
                                    if needs_refresh(other, prior_imports, open_numbers) or other in changed_relations),
                             timeline=False, repo=repo)
    # Finish every required fetch before spending the remaining request budget
    # on GitHub's optional asynchronous mergeability recalculation.
    mergeability_recheck = recheck_unknown_mergeability([*originals.values(), *imported.values()], repo)
    # A merge can occur while these bounded requests are in flight. Stamp only
    # fresh detail observations; terminal history keeps its original clock.
    checked_at = datetime.now(timezone.utc).isoformat()
    prior_prs = {**prior_sources, **prior_imports}
    refreshed = {}
    for number, raw in {**originals, **imported}.items():
        item = normalize(raw)
        item["checkedAt"] = checked_at
        item["mergeabilityObservation"] = remember_mergeability(item, prior_prs.get(number, {}), item["mergeabilityCheckedAt"])
        refreshed[number] = item
    prs = []
    for number in sorted(numbers):
        item = dict(refreshed[number] if number in originals else prior_sources[number])
        item.setdefault("checkedAt", previous.get("checkedAt"))
        if number in originals:
            item["importsComplete"] = policy == "none" or not originals[number]["timelineItems"]["pageInfo"]["hasPreviousPage"]
        imports = []
        for other, head in sorted(relations[number].items()):
            if other in imported:
                actual = import_source(imported[other], item["url"], item.get("headOwner"), item["ref"], repo)
                expected = timeline_relations.get(number, {}).get(other)
                if actual is None or (expected is not None and actual != expected):
                    raise RuntimeError(f"Import #{other} source footer changed during retrieval")
                head = actual
                linked = dict(refreshed[other])
            else:
                linked = dict(prior_imports[other])
                linked.setdefault("checkedAt", previous.get("checkedAt"))
            linked.update(sourceHead=head, matchesSourceHead=head == item["head"], relationshipEvidence=linked["url"])
            imports.append(linked)
        item["importPolicy"] = policy
        item["imports"] = imports
        prs.append(item)
    return prs, refreshed, prior_prs, {
        "openInventoryTotal": len(open_numbers), "retainedSources": len(numbers) - len(originals),
        "retainedImports": len(import_numbers) - len(imported), "mergeabilityRecheck": mergeability_recheck,
    }


def check_report(prs: list[dict], previous: dict) -> dict:
    """Report service failures without conflating old imports with current source."""
    prior = {(pr.get("repo", REPO), pr["number"]): pr
             for source in previous.get("prs", []) for pr in (source, *source.get("imports", []))}
    owners = {(node["repo"], node["number"]): node.get("followup", {})
              for node in REGISTRY["nodes"] if node["type"] == "pr"}
    failures, changes, imports, mergeability, mergeability_changes = [], [], [], [], []
    for source in prs:
        repo = source.get("repo", REPO)
        followup = owners.get((repo, source["number"]), {})
        assignment = {key: followup[key] for key in ("owner", "sourceHead", "disposition", "nextAction")
                      if key in followup}
        for pr in (source, *source.get("imports", [])):
            old = prior.get((repo, pr["number"]), {})
            is_source = pr is source
            checks = pr.get("checks") or {}
            failed = [check for check in checks.get("contexts", []) if check["state"] in FAILURES]
            old_checks = old.get("checks") or {}
            old_failed = [check for check in old_checks.get("contexts", []) if check["state"] in FAILURES]
            active = pr["status"] in {"open", "draft"}
            observation = {
                "repo": repo, "source": source["number"], "sourceHead": source["head"],
                "sourceStatus": source["status"], "number": pr["number"], "url": pr["url"],
                "head": pr["head"], "status": pr["status"], "checkedAt": pr.get("checkedAt"),
                "branchHead": pr.get("branchHead"),
                "sourceBranchMismatch": bool(pr.get("branchHead") and pr["branchHead"] != pr["head"]),
                "base": pr.get("base"), "mergeable": pr.get("mergeable"), "mergeState": pr.get("mergeState"),
                "mergeabilityCheckedAt": pr.get("mergeabilityCheckedAt"),
                "scope": ("source" if is_source else "current-source-import"
                          if pr["sourceHead"] == source["head"] else "older-source-import"),
                "checksState": checks.get("state"), "checksComplete": checks.get("complete", False),
                "failedChecks": failed,
            }
            if not is_source:
                observation["importedSourceHead"] = pr["sourceHead"]
                if active:
                    imports.append(observation)
            if active and (failed or check_states(pr) & FAILURES):
                failures.append({**observation, "followup": assignment})
            unresolved = pr.get("mergeable") in {"CONFLICTING", "UNKNOWN"} or observation["sourceBranchMismatch"]
            if active and unresolved:
                mergeability.append({**observation, "followup": assignment})
            if (unresolved or old.get("mergeable") in {"CONFLICTING", "UNKNOWN"}
                    or old.get("branchHead") and old["branchHead"] != old.get("head")) and any(
                pr.get(key) != old.get(key) for key in ("head", "branchHead", "base", "status", "mergeable", "mergeState")
            ):
                mergeability_changes.append({**observation, "followup": assignment})
            # Include resolution and closure, and re-report failures after a head
            # change even if the service reuses its status-context identifier.
            if (failed or old_failed or checks.get("state") in FAILURES or old_checks.get("state") in FAILURES) and (
                (pr["head"], pr["status"], pr.get("sourceHead"), checks) !=
                (old.get("head"), old.get("status"), old.get("sourceHead"), old_checks)
            ):
                changes.append(observation)
    return {"checkFailures": failures, "changedChecks": changes, "activeImports": imports,
            "mergeabilityIssues": mergeability, "changedMergeability": mergeability_changes}


def reapproval_reminders(post: bool = False) -> None:
    """Use the existing snapshot as an index, then refresh before each write."""
    snapshot = json.loads((ROOT / "github-status.json").read_text())
    nodes = {(node.get("repo"), node.get("number")): node for node in REGISTRY["nodes"]
             if node["type"] == "pr"}
    if post:
        identity = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                                  text=True, capture_output=True, check=True, timeout=30)
        if identity.stdout.strip() != OWNER:
            raise RuntimeError("Reminder credential does not belong to the configured owner")
    for pr in snapshot["prs"]:
        if pr.get("repo") != "google/gvisor" or pr.get("status") != "open":
            continue
        node = nodes.get((pr["repo"], pr["number"]), {})
        # Older snapshots lack author association. Refresh only recorded
        # amendments; missing proof is visible without spending write budget.
        if node.get("reapproval", {}).get("head") != pr["head"]:
            print(json.dumps(reminder_plan(pr, node, datetime.now(timezone.utc), OWNER)))
            continue
        raw = fetch_numbers([pr["number"]], timeline=False, repo=pr["repo"], team_details=False)[pr["number"]]
        recheck_unknown_mergeability([raw], repo=pr["repo"])
        current = normalize(raw)
        current["importPolicy"] = repository_policies(REGISTRY).get(pr["repo"])
        plan = reminder_plan(current, node, datetime.now(timezone.utc), OWNER)
        if current["head"] != pr["head"]:
            plan.update(status="skipped", reason="head-changed-since-snapshot")
        if not post or plan["status"] != "due":
            print(json.dumps(plan))
            continue
        # Never retry this mutation: an interrupted response may have posted.
        # The next scheduled run reads the exact marker from GitHub history.
        try:
            response = subprocess.run(
                ["gh", "api", "--method", "POST", f"repos/{pr['repo']}/issues/{pr['number']}/comments",
                 "--input", "-"], input=json.dumps({"body": plan["body"]}),
                text=True, capture_output=True, check=True, timeout=60,
            )
        except subprocess.CalledProcessError as error:
            detail = error.stderr.strip() if error.stderr else "gh returned no diagnostic"
            raise RuntimeError(f"Reminder POST for {pr['repo']}#{pr['number']} failed: {detail}; not retried") from None
        comment = json.loads(response.stdout)
        if comment.get("body") != plan["body"] or comment.get("user", {}).get("login") != OWNER:
            raise RuntimeError("Posted reminder readback does not match its planned body and owner")
        print(json.dumps({**plan, "status": "posted", "url": comment["html_url"],
                          "createdAt": comment["created_at"]}))
        # GitHub issue comments have no atomic expected-head precondition.
        # Retain any observed post/write race explicitly; never assert approval.
        after = fetch_numbers([pr["number"]], timeline=False, repo=pr["repo"], team_details=False)[pr["number"]]
        if after["headRefOid"] != current["head"] or after["state"] != "OPEN":
            raise RuntimeError(f"PR #{pr['number']} changed while reminder {comment['html_url']} was posted")


def main() -> None:
    policies = repository_policies(REGISTRY)
    output = ROOT / "github-status.json"
    previous = {}
    if output.exists():
        previous = json.loads(output.read_text())
        if not (previous.get("schema") == 1 and previous.get("repo") == REPO and previous.get("owner") == OWNER):
            previous = {}
    prs, refreshed, prior_prs, inventories = [], {}, {}, {}
    for repo, policy in policies.items():
        records, fresh, prior, inventory = collect_repository(repo, policy, previous)
        prs.extend(records)
        refreshed.update({(repo, number): value for number, value in fresh.items()})
        prior_prs.update({(repo, number): value for number, value in prior.items()})
        inventories[repo] = inventory
    issue_fields = []
    issue_ids = {}
    for index, node in enumerate(REGISTRY["nodes"]):
        if node["type"] != "issue" or not node.get("repo") or not node.get("number"):
            continue
        owner, name = node["repo"].split("/")
        alias = f"i{index}"
        issue_fields.append(f'{alias}:repository(owner:"{owner}",name:"{name}"){{issue(number:{node["number"]}){{state updatedAt}}}}')
        issue_ids[alias] = node["id"]
    issues = {}
    if issue_fields:
        response = graphql("query{" + " ".join(issue_fields) + "}")
        for alias, value in response.items():
            if value and value["issue"]:
                issue = value["issue"]
                issues[issue_ids[alias]] = {"githubState": issue["state"].lower(), "updatedAt": issue["updatedAt"]}
    checked_at = datetime.now(timezone.utc).isoformat()
    snapshot = {"schema": 1, "repo": REPO, "owner": OWNER, "checkedAt": checked_at,
                "registryDate": REGISTRY["meta"]["updatedAt"], "repositories": policies, "prs": prs, "issues": issues,
                "workStates": record_states(prs, REGISTRY, previous, checked_at),
                "limits": {"requests": requests, "graphqlCost": query_cost, "graphqlRemaining": rate_remaining,
                           "nestedPageSize": 100, "maxOpenPRs": 200, "repositories": inventories,
                           **{key: sum(item[key] for item in inventories.values())
                              for key in ("openInventoryTotal", "retainedSources", "retainedImports")}}}
    temporary = ROOT / ".github-status.json.tmp"
    temporary.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    # Run the same validator used by the browser before publishing any bytes.
    validation = subprocess.run(["node", str(ROOT / "metadata-preflight.mjs"), str(ROOT), str(temporary)],
                                check=False, capture_output=True, text=True, timeout=30)
    if validation.returncode:
        temporary.unlink()
        raise RuntimeError(f"Client metadata validation failed: {validation.stderr.strip()}")
    temporary.replace(output)
    print(f"Retained {len(prs)} PRs across {len(policies)} repositories; refreshed "
          f"{len(refreshed)} records in {requests} GitHub requests ({query_cost} GraphQL points).")
    # gh run view scans lines with Go's default 64 KiB token limit. A single
    # aggregate report can hide the rest of this step, including human feedback.
    for kind, observations in check_report(prs, previous).items():
        for observation in observations:
            print(json.dumps({kind: [observation]}))
        if not observations:
            print(json.dumps({kind: []}))
    for event in review_watch_report(prs, previous):
        print(json.dumps({"reviewSubscriptions": [event]}))
    for source in prs:
        for pr in (source, *source["imports"]):
            if (pr.get("repo", REPO), pr["number"]) not in refreshed:
                continue
            changes = changed_feedback(pr, prior_prs.get((pr.get("repo", REPO), pr["number"]), {}))
            if changes or not pr["feedback"]["complete"]:
                print(json.dumps({"repo": pr.get("repo", REPO), "number": pr["number"], "url": pr["url"], "head": pr["head"],
                                  "feedbackComplete": pr["feedback"]["complete"], "feedback": changes}))
            references = pr.get("crossReferences")
            changed = changed_references(pr, prior_prs.get((pr.get("repo", REPO), pr["number"]), {}))
            if references and (changed or not references["complete"]):
                print(json.dumps({"repo": pr.get("repo", REPO), "number": pr["number"], "url": pr["url"],
                                  "crossReferencesComplete": references["complete"], "crossReferences": changed}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reapproval-reminders", choices=("plan", "post"),
                        help="Use the existing snapshot and fresh GitHub reads for approved amendments")
    arguments = parser.parse_args()
    if arguments.reapproval_reminders:
        reapproval_reminders(post=arguments.reapproval_reminders == "post")
    else:
        main()
