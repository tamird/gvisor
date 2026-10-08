"""Derive overlapping work states and retain head-bound observation ages."""

from typing import Literal, TypedDict


class ScopedState(TypedDict):
    scope: str | None


class StateEvidence(ScopedState, total=False):
    transitionAt: str | None
    qualifier: str


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


def check_states(pr: dict) -> set[str]:
    checks = pr.get("checks") or {}
    return {state for state in [checks.get("state"), *(item["state"] for item in checks.get("contexts", []))]
            if isinstance(state, str)}


def pr_states(pr: dict) -> dict[str, StateEvidence]:
    """Never infer import readiness from an approval or a green check alone."""
    head = pr["head"]
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
        states["failing"] = {"scope": ",".join(f"{item['number']}:{item['head']}" for item in failures)}
    if pending:
        states["checks-pending"] = {"scope": ",".join(f"{item['number']}:{item['head']}" for item in pending)}
    if pr.get("mergeable") == "CONFLICTING":
        states["conflicts"] = {"scope": head}
    if pr["status"] == "draft":
        states["draft"] = {"scope": head}
    elif pr.get("reviewDecision") == "CHANGES_REQUESTED":
        states["changes-requested"] = {"scope": head}
    elif pr.get("reviewDecision") != "APPROVED":
        states["review"] = {"scope": head}
    if active:
        states["importing"] = {"scope": ",".join(f"{item['number']}:{item['head']}" for item in active)}
    elif any(item["status"] == "merged" for item in imports):
        states["imported"] = {"scope": head}
    elif (pr["status"] == "open" and pr.get("reviewDecision") != "CHANGES_REQUESTED"
          and (pr.get("reviewDecision") == "APPROVED" or
               (pr.get("labelsComplete") and "ready to pull" in pr.get("labels", [])))
          and pr.get("importsComplete")):
        checks = pr.get("checks")
        qualifier = ("Checks unknown" if not checks or not checks.get("complete") else
                     "Checks failing" if failures else "Checks pending" if pending else
                     "Checks passed" if checks["state"] == "SUCCESS" else "Checks unknown")
        states["awaiting-import"] = {"scope": head, "qualifier": qualifier}
    return states


def derive_states(prs: list[dict], registry: dict) -> StateMap:
    states = {f"pr:{pr['number']}": pr_states(pr) for pr in prs}
    promoted = {pr["ref"] for pr in prs if pr["status"] in {"open", "draft", "merged"}}
    for node in registry["nodes"]:
        if node["type"] != "branch" or node.get("ref") in promoted:
            continue
        # This is an explicit, curated request, never inferred from prose.
        if node["status"].split(";")[0] == "contributor review requested":
            states[node["id"]] = {"review": {"scope": node.get("head")}}
    return states


def record_states(prs: list[dict], registry: dict, previous: dict, checked_at: str) -> ObservationMap:
    current = derive_states(prs, registry)
    # Backfill only the immediately preceding verified PR observation. Curated
    # branch history cannot be reconstructed from the current registry.
    prior_prs = {f"pr:{pr['number']}": pr_states(pr) for pr in previous.get("prs", [])}
    history = previous.get("workStates", {})
    result: ObservationMap = {}
    for identity, states in current.items():
        result[identity] = {}
        for state, evidence in states.items():
            old = history.get(identity, {}).get(state, {})
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
