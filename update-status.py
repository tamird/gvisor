#!/usr/bin/env python3
"""Publish public GitHub metadata using the existing authenticated gh session."""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from work_states import check_purpose, effective_approval, record_states, remember_mergeability, review_decision


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
      ... on CheckRun { name status conclusion detailsUrl checkSuite { app { name slug } workflowRun { workflow { name } } } }
      ... on StatusContext { context state targetUrl }
    }
  }
} } } }
"""
FIELDS = """
number title url state isDraft headRefName headRefOid baseRefOid updatedAt createdAt mergedAt closedAt
headRepository { owner { login } } repository { nameWithOwner }
author { login } reviewDecision mergeable mergeStateStatus
labels(first:100) { nodes { name } pageInfo { hasNextPage } }
comments(last:100) { pageInfo { hasPreviousPage } nodes {
  id url body createdAt updatedAt author { login __typename }
} }
reviews(last:100) { pageInfo { hasPreviousPage } nodes {
  id url body state submittedAt updatedAt author { login __typename } commit { oid }
} }
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
  nodes { ... on CrossReferencedEvent { source {
    ... on PullRequest { number url body author { login } repository { nameWithOwner } }
  } } }
}
headHistory:timelineItems(last:1,itemTypes:[HEAD_REF_FORCE_PUSHED_EVENT]) {
  nodes { ... on HeadRefForcePushedEvent { createdAt afterCommit { oid } } }
}
"""


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
            raise RuntimeError("GitHub query timed out; previous file is unchanged") from None
        if result.returncode:
            # Retry this read-only query once for an interrupted transport.
            interrupted = any(error in result.stderr for error in (
                "net/http: TLS handshake timeout", "unexpected EOF", "gh: HTTP 499",
                "i/o timeout",
            ))
            if attempt == 0 and requests < REQUEST_LIMIT and interrupted:
                print("GitHub transport interrupted; retrying once within the request cap", file=sys.stderr)
                continue
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


def normalize(pr: dict) -> dict:
    commits = pr["commits"]["nodes"]
    if len(commits) != 1 or commits[0]["commit"]["oid"] != pr["headRefOid"]:
        raise RuntimeError(f"PR #{pr['number']} head changed during retrieval")
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
                workflow = ((suite.get("workflowRun") or {}).get("workflow") or {}).get("name")
                # This repository-owned workflow requests reviewers; it does not
                # validate contributor source. Keep its raw outcome observable.
                context = {"name": item["name"], "state": state or "UNKNOWN",
                                 "url": public_url(item["detailsUrl"]), "kind": "check-run",
                                 "app": app.get("name"), "appSlug": app.get("slug"),
                                 "workflow": workflow}
                context["purpose"] = check_purpose(context)
                contexts.append(context)
            else:
                contexts.append({"name": item["context"], "state": item["state"],
                                 "url": public_url(item["targetUrl"]), "app": None, "kind": "status",
                                 "purpose": "validation"})
        checks = {"state": rollup["state"], "total": connection["totalCount"],
                  "complete": not connection["pageInfo"]["hasNextPage"], "contexts": contexts}
    threads = pr["reviewThreads"]
    normalized = {
        "number": pr["number"], "title": pr["title"], "url": pr["url"],
        "status": "draft" if pr["isDraft"] and pr["state"] == "OPEN" else pr["state"].lower(),
        "ref": pr["headRefName"], "head": pr["headRefOid"], "base": pr["baseRefOid"],
        "headCommittedAt": commits[0]["commit"]["committedDate"],
        "headHistoryComplete": "headHistory" in pr,
        "headIntroducedAt": max((event["createdAt"] for event in pr.get("headHistory", {}).get("nodes", [])
                                 if (event.get("afterCommit") or {}).get("oid") == pr["headRefOid"]), default=None),
        "headOwner": (pr["headRepository"] or {}).get("owner", {}).get("login"),
        "updatedAt": pr["updatedAt"], "createdAt": pr["createdAt"],
        "mergedAt": pr["mergedAt"], "closedAt": pr["closedAt"],
        "githubReviewDecision": pr["reviewDecision"],
        "reviewsComplete": not pr["reviews"]["pageInfo"]["hasPreviousPage"],
        "reviewRequests": [item["requestedReviewer"] for item in pr["reviewRequests"]["nodes"] if item["requestedReviewer"]],
        "reviewRequestsComplete": not pr["reviewRequests"]["pageInfo"]["hasNextPage"],
        "mergeable": pr["mergeable"],
        "mergeabilityCheckedAt": pr["_retrievedAt"],
        "mergeState": pr["mergeStateStatus"],
        "labels": [label["name"] for label in pr["labels"]["nodes"]],
        "labelsComplete": not pr["labels"]["pageInfo"]["hasNextPage"],
        "threads": {"unresolved": sum(not thread["isResolved"] for thread in threads["nodes"]),
                    "total": threads["totalCount"], "complete": not threads["pageInfo"]["hasNextPage"]},
        "feedback": feedback(pr),
        "checks": checks,
    }

    normalized["reviewDecision"] = review_decision(normalized)
    normalized["approvedHead"] = normalized["head"] if normalized["reviewDecision"] == "APPROVED" else None
    approval = effective_approval(normalized)
    normalized["approvedAt"] = approval["submittedAt"] if approval else None
    return normalized


def fetch_numbers(numbers: list[int], timeline: bool) -> dict[int, dict]:
    result = {}
    owner, name = REPO.split("/")
    for start in range(0, len(numbers), PR_BATCH_SIZE):
        fields = " ".join(f"p{number}:pullRequest(number:{number}){{{FIELDS}{TIMELINE if timeline else 'body'}}}"
                          for number in numbers[start:start + PR_BATCH_SIZE])
        response = graphql(f'query {{ repository(owner:"{owner}",name:"{name}") {{ {fields} }} }}')
        for pr in response["repository"].values():
            if pr is None:
                raise RuntimeError("A tracked public PR was unavailable; previous snapshot is unchanged")
            pr["_retrievedAt"] = datetime.now(timezone.utc).isoformat()
            result[pr["number"]] = pr
    return result


def import_source(candidate: dict, original: dict) -> str | None:
    if candidate.get("repository", {}).get("nameWithOwner") != REPO or (candidate.get("author") or {}).get("login") != "copybara-service":
        return None
    head_owner = (original["headRepository"] or {}).get("owner", {}).get("login")
    footer = re.compile(r"FUTURE_COPYBARA_INTEGRATE_REVIEW=" + re.escape(original["url"])
                        + " from " + re.escape(f"{head_owner}:{original['headRefName']}")
                        + r" ([0-9a-f]{40})")
    heads = {match[1] for line in candidate["body"].splitlines() if (match := footer.fullmatch(line.strip()))}
    if len(heads) > 1:
        raise RuntimeError(f"Import #{candidate['number']} contains ambiguous source revisions")
    return next(iter(heads), None)


def main() -> None:
    # Search only the public author's open PRs; retained closed PRs are fetched
    # individually in small GraphQL batches. No per-PR REST request loop.
    numbers = {node["number"] for node in REGISTRY["nodes"]
               if node["type"] == "pr" and node["repo"] == REPO}
    output = ROOT / "github-status.json"
    previous = {}
    if output.exists():
        previous = json.loads(output.read_text())
        if previous.get("schema") == 1 and previous.get("repo") == REPO and previous.get("owner") == OWNER:
            numbers.update(pr["number"] for pr in previous["prs"] if isinstance(pr.get("number"), int))
        else:
            previous = {}
    cursor = None
    open_numbers = set()
    total = None
    for _ in range(2):
        query = """query($q:String!,$after:String) {
          search(query:$q,type:ISSUE,first:100,after:$after) {
            issueCount pageInfo { hasNextPage endCursor }
            nodes { ... on PullRequest { number } }
          }
        }"""
        found = graphql(query, {"q": f"repo:{REPO} is:pr is:open author:{OWNER}", "after": cursor})["search"]
        if found["issueCount"] > 200:
            raise RuntimeError("Open PR inventory exceeds the documented 200-PR bound")
        if total is not None and total != found["issueCount"]:
            raise RuntimeError("Open PR inventory changed during retrieval")
        total = found["issueCount"]
        for item in found["nodes"]:
            if item["number"] in open_numbers:
                raise RuntimeError("Duplicate open PR in paginated search")
            open_numbers.add(item["number"])
        if not found["pageInfo"]["hasNextPage"]:
            break
        cursor = found["pageInfo"]["endCursor"]
    else:
        raise RuntimeError("Open PR inventory is incomplete")
    if len(open_numbers) != total:
        raise RuntimeError("Open PR inventory is incomplete")
    numbers.update(open_numbers)
    originals = fetch_numbers(sorted(numbers), timeline=True)
    relations = {}
    for number, pr in originals.items():
        # A bot-authored import must explicitly name this original PR and its
        # exact head owner/ref. A differing SHA is a verified but stale import.
        matches = {}
        for event in pr["timelineItems"]["nodes"]:
            candidate = event.get("source", {})
            if head := import_source(candidate, pr):
                matches[candidate["number"]] = head
        relations[number] = matches
    imported = fetch_numbers(sorted({number for matches in relations.values() for number in matches}), timeline=False)
    prs = []
    for number, pr in originals.items():
        for other, head in relations[number].items():
            if import_source(imported[other], pr) != head:
                raise RuntimeError(f"Import #{other} source footer changed during retrieval")
        item = normalize(pr)
        item["importsComplete"] = not pr["timelineItems"]["pageInfo"]["hasPreviousPage"]
        item["imports"] = [{**normalize(imported[other]), "sourceHead": head,
                            "matchesSourceHead": head == pr["headRefOid"],
                            "relationshipEvidence": imported[other]["url"]}
                           for other, head in relations[number].items()]
        prs.append(item)
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
    # A merge can occur while these bounded requests are in flight. Stamp the
    # completed observation, so its exact transition never appears in the future.
    checked_at = datetime.now(timezone.utc).isoformat()
    prior_prs = {pr["number"]: pr for source in previous.get("prs", [])
                 for pr in (source, *source.get("imports", []))}
    for source in prs:
        for item in (source, *source["imports"]):
            item["checkedAt"] = checked_at
            item["mergeabilityObservation"] = remember_mergeability(item, prior_prs.get(item["number"], {}), item["mergeabilityCheckedAt"])
    snapshot = {"schema": 1, "repo": REPO, "owner": OWNER, "checkedAt": checked_at,
                "registryDate": REGISTRY["meta"]["updatedAt"], "prs": prs, "issues": issues,
                "workStates": record_states(prs, REGISTRY, previous, checked_at),
                "limits": {"requests": requests, "graphqlCost": query_cost, "graphqlRemaining": rate_remaining, "nestedPageSize": 100, "maxOpenPRs": 200}}
    temporary = ROOT / ".github-status.json.tmp"
    temporary.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    # Run the same validator used by the browser before publishing any bytes.
    validation = subprocess.run(["node", str(ROOT / "metadata-preflight.mjs"), str(ROOT), str(temporary)],
                                check=False, capture_output=True, text=True, timeout=30)
    if validation.returncode:
        temporary.unlink()
        raise RuntimeError(f"Client metadata validation failed: {validation.stderr.strip()}")
    temporary.replace(output)
    print(f"Updated {len(prs)} PRs and {len(imported)} verified import PRs in {requests} GitHub requests ({query_cost} GraphQL points).")
    for source in prs:
        for pr in (source, *source["imports"]):
            changes = changed_feedback(pr, prior_prs.get(pr["number"], {}))
            if changes or not pr["feedback"]["complete"]:
                print(json.dumps({"number": pr["number"], "url": pr["url"], "head": pr["head"],
                                  "feedbackComplete": pr["feedback"]["complete"], "feedback": changes}))


if __name__ == "__main__":
    main()
