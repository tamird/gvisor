"""Derive overlapping work states and retain revision-bound observations."""

from typing import Literal, TypedDict


class ScopedState(TypedDict):
    scope: str | None


class StateEvidence(ScopedState, total=False):
    transitionAt: str | None
    qualifier: str | None


class Observation(ScopedState):
    since: str | None
    basis: Literal["observed", "transition", "unknown"]
    qualifier: str | None


class StateObservation(Observation, total=False):
    evidence: str


StateMap = dict[str, dict[str, StateEvidence]]
ObservationMap = dict[str, dict[str, StateObservation]]

FAILURES = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE"}
PENDING = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
PASSED = {"SUCCESS", "SKIPPED", "NEUTRAL"}


def check_states(pr: dict) -> set[str]:
    checks = pr.get("checks") or {}
    return {state for state in [checks.get("state"), *(item["state"] for item in checks.get("contexts", []))]
            if isinstance(state, str)}


def checks_passed(pr: dict) -> bool:
    checks = pr.get("checks") or {}
    return bool(checks.get("complete") and checks.get("total", 0) > 0
                and len(checks.get("contexts", [])) == checks["total"]
                and checks.get("state") == "SUCCESS"
                and check_states(pr) <= PASSED)


def check_qualifier(pr: dict) -> str:
    states = check_states(pr)
    return ("Checks failing" if states & FAILURES else "Checks pending" if states & PENDING
            else "Checks passed" if checks_passed(pr) else "Checks unknown")


def review_decision(pr: dict) -> str | None:
    """Only current-head, still-effective reviews can supply approval credit."""
    decision = pr.get("githubReviewDecision", pr.get("reviewDecision"))
    if decision != "APPROVED":
        return decision
    if not pr.get("reviewsComplete") or not pr.get("reviewRequestsComplete"):
        return "REVIEW_REQUIRED"
    requested = {item.get("login") for item in pr.get("reviewRequests", []) if item.get("login")}
    latest = {}
    for item in pr.get("feedback", {}).get("items", []):
        author = (item.get("author") or {}).get("login")
        if (item.get("kind") != "review" or not author or not item.get("submittedAt")
                or item.get("state") not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}):
            continue
        if author not in latest or item["submittedAt"] > latest[author]["submittedAt"]:
            latest[author] = item
    if any(author not in requested and item["state"] == "APPROVED"
           and (item.get("commit") or {}).get("oid") == pr["head"]
           for author, item in latest.items()):
        return "APPROVED"
    return "REVIEW_REQUIRED"


class MergeabilityObservation(TypedDict):
    state: Literal["MERGEABLE", "CONFLICTING"]
    head: str
    base: str | None
    checkedAt: str


def remember_mergeability(pr: dict, previous: dict, checked_at: str) -> MergeabilityObservation | None:
    """UNKNOWN is a pending calculation, not evidence clearing a conflict."""
    if pr.get("mergeable") in {"MERGEABLE", "CONFLICTING"}:
        return {"state": pr["mergeable"], "head": pr["head"],
                "base": pr.get("base"), "checkedAt": checked_at}
    if observation := previous.get("mergeabilityObservation"):
        return observation
    if previous.get("mergeable") in {"MERGEABLE", "CONFLICTING"}:
        return {"state": previous["mergeable"], "head": previous["head"],
                "base": previous.get("base"), "checkedAt": previous["checkedAt"]}
    return None


def conflict_qualifier(pr: dict) -> str | None:
    previous = pr.get("mergeabilityObservation")
    if (pr.get("mergeable") == "UNKNOWN" and previous
            and previous["state"] == "CONFLICTING" and previous["head"] == pr["head"]):
        return f"Last observed conflict at {previous['checkedAt']}; GitHub is recomputing current mergeability"
    return None


def pr_states(pr: dict) -> dict[str, StateEvidence]:
    """Keep source review, public import progress and check provenance distinct."""
    head = pr["head"]
    decision = review_decision(pr)
    if pr["status"] in {"merged", "closed"}:
        timestamp = pr.get("mergedAt" if pr["status"] == "merged" else "closedAt")
        return {pr["status"]: {"scope": head, "transitionAt": timestamp}}
    states: dict[str, StateEvidence] = {}
    imports = [item for item in pr.get("imports", []) if item["matchesSourceHead"]]
    active = [item for item in imports if item["status"] in {"open", "draft"}]
    visible = [pr, *active]
    failures = [item for item in visible if check_states(item) & FAILURES]
    pending = [item for item in visible if check_states(item) & PENDING]
    if failures:
        origins = ["Source checks failing" if item is pr else f"Import #{item['number']} failing" for item in failures]
        states["failing"] = {"scope": ",".join(f"{item['number']}:{item['head']}" for item in failures),
                             "qualifier": "; ".join(origins)}
    if pending:
        states["checks-pending"] = {"scope": ",".join(f"{item['number']}:{item['head']}" for item in pending)}
    if pr.get("mergeable") == "CONFLICTING" or conflict_qualifier(pr):
        states["conflicts"] = {"scope": head, "qualifier": conflict_qualifier(pr)}
    if pr["status"] == "draft":
        states["draft"] = {"scope": head}
    elif decision == "CHANGES_REQUESTED":
        states["changes-requested"] = {"scope": head}
    elif decision != "APPROVED":
        states["maintainer-review"] = {"scope": head, "qualifier": "No effective approval for the current head" if pr.get("githubReviewDecision", pr.get("reviewDecision")) == "APPROVED" else None}
    eligible = pr["status"] == "open" and decision == "APPROVED"
    if active:
        qualifiers = []
        for item in active:
            merge = {"CONFLICTING": "conflicts", "MERGEABLE": "no reported conflict"}.get(item.get("mergeable"), "mergeability unknown")
            qualifiers.append(f"#{item['number']}: {item['status']}, {check_qualifier(item).lower()}, {merge}")
        states["importing"] = {"scope": ",".join(f"{item['number']}:{item['head']}" for item in active),
                               "qualifier": "; ".join(qualifiers)}
        ready = [item for item in active if item["status"] == "open"
                 and item.get("mergeable") == "MERGEABLE"
                 and review_decision(item) != "CHANGES_REQUESTED" and checks_passed(item)]
        if (eligible and pr.get("importsComplete") and pr.get("mergeable") == "MERGEABLE"
                and checks_passed(pr) and len(ready) == len(active)):
            states["waiting-merge"] = {"scope": head + "," + ",".join(f"{item['number']}:{item['head']}" for item in ready),
                                      "qualifier": ", ".join(f"Import #{item['number']}" for item in ready)}
    elif any(item["status"] == "merged" for item in imports):
        states["imported"] = {"scope": head}
    elif eligible and pr.get("importsComplete"):
        states["awaiting-import"] = {"scope": head, "qualifier": check_qualifier(pr)}
    return states


def derive_states(prs: list[dict], registry: dict) -> StateMap:
    states = {f"pr:{pr['number']}": pr_states(pr) for pr in prs}
    promoted = {pr["ref"] for pr in prs if pr["status"] in {"open", "draft", "merged"}}
    closed = {f"pr:{pr['number']}" for pr in prs if pr["status"] in {"closed", "merged"}}
    for node in registry["nodes"]:
        if node["type"] != "branch" or node["id"] in closed or node.get("ref") in promoted:
            continue
        # Only the task owner records actual pending contributor decisions.
        # An unpublished amendment is its own branch, not the live PR's state.
        decision = node.get("contributorReview")
        if decision and decision["status"] == "pending":
            states.setdefault(node["id"], {})["contributor-review"] = {"scope": decision["head"]}
    return states


def record_states(prs: list[dict], registry: dict, previous: dict, checked_at: str) -> ObservationMap:
    current = derive_states(prs, registry)
    prior_prs = {f"pr:{pr['number']}": pr_states(pr) for pr in previous.get("prs", [])}
    # The old collector's aggregate approval cannot backdate the first
    # observation under the exact-head rule.
    for pr in previous.get("prs", []):
        if pr.get("reviewDecision") == "APPROVED" and "approvedHead" not in pr:
            prior_prs[f"pr:{pr['number']}"].pop("maintainer-review", None)
    history = previous.get("workStates", {})
    result: ObservationMap = {}
    for identity, states in current.items():
        result[identity] = {}
        for state, evidence in states.items():
            old = history.get(identity, {}).get(state, {})
            # Legacy PR review meant maintainer review. Legacy branch review
            # was emitted only for an explicit contributor-review-requested
            # status; require the same candidate and a still-pending decision.
            if not old and ((state == "maintainer-review" and identity.startswith("pr:"))
                            or (state == "contributor-review" and identity.startswith("branch:"))):
                old = history.get(identity, {}).get("review", {})
            since: str | None
            basis: Literal["observed", "transition", "unknown"]
            if evidence["scope"] is None:
                since, basis = None, "unknown"
            elif evidence.get("transitionAt"):
                since, basis = evidence["transitionAt"], "transition"
            elif old.get("scope") == evidence["scope"] and old.get("since"):
                since, basis = old["since"], old["basis"]
            elif prior_prs.get(identity, {}).get(state, {}).get("scope") == evidence["scope"]:
                since, basis = previous["checkedAt"], "observed"
            else:
                since, basis = checked_at, "observed"
            result[identity][state] = {"scope": evidence["scope"], "since": since, "basis": basis,
                                       "qualifier": evidence.get("qualifier")}
            if old.get("scope") == evidence["scope"] and old.get("evidence"):
                result[identity][state]["evidence"] = old["evidence"]
    return result
