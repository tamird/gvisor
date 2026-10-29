"""Derive overlapping work states and retain revision-bound observations."""

from datetime import datetime
import re
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
    observedAt: str


StateMap = dict[str, dict[str, StateEvidence]]
ObservationMap = dict[str, dict[str, StateObservation]]

FAILURES = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE"}
PENDING = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
PASSED = {"SUCCESS", "SKIPPED", "NEUTRAL"}


def repository_policies(registry: dict) -> dict[str, str]:
    """Keep import workflows explicit; old snapshots describe the primary repo."""
    policies = registry["meta"].get("repositories", {registry["meta"]["repo"]: "copybara"})
    if not isinstance(policies, dict) or registry["meta"]["repo"] not in policies or not all(
        isinstance(repo, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
        and policy in ("copybara", "none") for repo, policy in policies.items()
    ):
        raise ValueError("Invalid tracked repository policies")
    registered_only = registry["meta"].get("registeredOnlyRepositories", [])
    if (not isinstance(registered_only, list)
            or not all(isinstance(repo, str) and policies.get(repo) == "none" for repo in registered_only)
            or len(set(registered_only)) != len(registered_only)):
        raise ValueError("Invalid registered-only repository selection")
    return policies


def pr_identity(pr: dict, primary: str) -> str:
    repo = pr.get("repo", primary)
    # Preserve existing gVisor deep links and state history.
    return f"pr:{pr['number']}" if repo == primary else f"pr:{repo}:{pr['number']}"


def check_purpose(check: dict) -> Literal["validation", "reviewer-assignment"]:
    """Classify the repository's reviewer workflow from its service identity."""
    return ("reviewer-assignment"
            if check.get("kind") == "check-run" and check.get("appSlug") == "github-actions"
            and check.get("workflow") == "Auto Assign" and check.get("name") == "assign"
            else "validation")


def is_import_status(check: dict) -> bool:
    return check.get("kind") == "status" and check.get("name") in {"import/copybara", "feedback/copybara"}


def is_approval_gate(pr: dict, check: dict) -> bool:
    """Recognize BuildBuddy's exact provider response before CI can run."""
    return (pr.get("repo") == "buildbuddy-io/buildbuddy" and check.get("kind") == "status"
            and check.get("state") == "ERROR" and check.get("description") == "Check requires approving review"
            and check.get("url") in {"https://app.buildbuddy.io", "https://app.buildbuddy.io/",
                                     "https://app.buildbuddy.dev", "https://app.buildbuddy.dev/"})


def is_canceled_buildkite_run(check: dict) -> bool:
    build = re.fullmatch(r"Build #(\d+) cancel(?:l)?ed by \S.*", check.get("description") or "")
    name = check.get("name", "")
    return bool(check.get("kind") == "status" and check.get("state") == "FAILURE" and build
                and name.startswith("buildkite/")
                and check.get("url") == f"https://buildkite.com/gvisor/{name.removeprefix('buildkite/')}/builds/{build[1]}")


def complete_checks(pr: dict) -> bool:
    checks = pr.get("checks") or {}
    return bool(checks.get("complete") and checks.get("total", 0) > 0
                and len(checks.get("contexts", [])) == checks["total"])


def validation_checks(pr: dict) -> list[dict]:
    return [item for item in (pr.get("checks") or {}).get("contexts", [])
            if item.get("purpose") != "reviewer-assignment" and not is_import_status(item)
            and not is_approval_gate(pr, item)]


def check_states(pr: dict) -> set[str]:
    checks = pr.get("checks") or {}
    states = {item["state"] for item in validation_checks(pr) if not is_canceled_buildkite_run(item)}
    aggregate = checks.get("state")
    administrative = {item["state"] for item in checks.get("contexts", [])
                      if item.get("purpose") == "reviewer-assignment" or is_import_status(item)
                      or is_approval_gate(pr, item) or is_canceled_buildkite_run(item)}
    explained_states = administrative | {item["state"] for item in checks.get("supersededContexts", [])}
    explained = complete_checks(pr) and (
        aggregate in FAILURES and bool(explained_states & FAILURES)
        or aggregate in PENDING and bool(explained_states & PENDING))
    # Preserve aggregate problems unless the complete inventory explains them
    # with an administrative, canceled, or superseded result of the same class.
    if isinstance(aggregate, str) and not explained:
        states.add(aggregate)
    return states


def checks_passed(pr: dict) -> bool:
    # Keep merge-readiness conservative: separating an administrative failure
    # from source validation does not establish that all service gates passed.
    checks = pr.get("checks") or {}
    return bool(complete_checks(pr) and validation_checks(pr)
                and checks.get("state") == "SUCCESS"
                and all(item["state"] in PASSED for item in checks["contexts"]))


def check_qualifier(pr: dict) -> str:
    states = check_states(pr)
    canceled = any(is_canceled_buildkite_run(item) for item in validation_checks(pr))
    validation = ("Checks failing" if states & FAILURES else "Checks canceled" if canceled else "Checks pending" if states & PENDING
                  else "Validation checks passed" if complete_checks(pr) and validation_checks(pr) and states <= PASSED
                  else "Checks unknown")
    services = {item["name"]: item for item in (pr.get("checks") or {}).get("contexts", [])
                if is_import_status(item)}
    if not services:
        return validation
    qualifiers = []
    for name, label in (("feedback/copybara", "Internal checks"),
                        ("import/copybara", "Internal import")):
        item = services.get(name, {})
        if item.get("state") in FAILURES:
            qualifiers.append(label + " failed" + ("; no public logs" if not item.get("url") else ""))
        elif item.get("state") in PENDING:
            qualifiers.append(label + " pending")
    if services.get("import/copybara", {}).get("state") == "SUCCESS":
        qualifiers.append("Internal import succeeded")
    if services.get("feedback/copybara", {}).get("state") == "SUCCESS":
        qualifiers.append("Internal checks reported passing")
    qualifiers.append("public " + validation.lower())
    return "; ".join(qualifiers)


def effective_approval(pr: dict, *, require_aggregate: bool = True,
                       trusted_reviewers: frozenset[str] = frozenset()) -> dict | None:
    """Return a current-head approval submitted after the head's time evidence."""
    decision = pr.get("githubReviewDecision", pr.get("reviewDecision"))
    if require_aggregate and decision != "APPROVED":
        return None
    if not pr.get("reviewsComplete") or not pr.get("reviewRequestsComplete") or not pr.get("headHistoryComplete"):
        return None
    cutoff_values = [pr.get("headCommittedAt"), pr.get("headIntroducedAt")]
    cutoff_values = [value for value in cutoff_values if value is not None]
    if not cutoff_values:
        return None
    try:
        cutoffs = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in cutoff_values]
    except (AttributeError, ValueError):
        return None
    if any(value.tzinfo is None for value in cutoffs):
        return None
    cutoff = max(cutoffs)
    requested = {item.get("login") for item in pr.get("reviewRequests", []) if item.get("login")}
    latest = {}
    for item in pr.get("feedback", {}).get("items", []):
        author = (item.get("author") or {}).get("login")
        if (item.get("kind") != "review" or not author or not item.get("submittedAt")
                or item.get("state") not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}):
            continue
        if author not in latest or item["submittedAt"] > latest[author]["submittedAt"]:
            latest[author] = item
    approvals = []
    for author, item in latest.items():
        if not require_aggregate and (
                (item.get("author") or {}).get("__typename") != "User"
                or author == pr.get("authorLogin", pr.get("headOwner"))
                or item.get("authorAssociation") not in {"MEMBER", "OWNER", "COLLABORATOR"}
                and author.casefold() not in trusted_reviewers):
            continue
        if (require_aggregate and author in requested or item["state"] != "APPROVED"
                or (item.get("commit") or {}).get("oid") != pr["head"]):
            continue
        try:
            submitted = datetime.fromisoformat(item["submittedAt"].replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            continue
        if submitted.tzinfo is not None and submitted >= cutoff:
            approvals.append(item)
    return max(approvals, key=lambda item: item["submittedAt"], default=None)


def review_decision(pr: dict) -> str | None:
    decision = pr.get("githubReviewDecision", pr.get("reviewDecision"))
    if decision != "APPROVED":
        return decision
    return "APPROVED" if effective_approval(pr) else "REVIEW_REQUIRED"


def approved_patch(pr: dict, evidence: dict | None) -> bool:
    """Use a reviewed mechanical comparison without inventing a fresh review."""
    if (not evidence or pr.get("repo", "google/gvisor") != "google/gvisor"
            or evidence.get("head") != pr.get("head") or pr.get("revisionChanged")
            or evidence.get("approvedHead") == evidence.get("head")
            or evidence.get("method") not in {"stable-patch-id", "reviewed-context-only"}
            or not all(isinstance(evidence.get(key), str) and re.fullmatch(r"[a-f0-9]{40}", evidence[key])
                       for key in ("head", "approvedHead", "oldPatchId", "newPatchId"))
            or evidence["method"] == "stable-patch-id" and evidence["oldPatchId"] != evidence["newPatchId"]
            or pr.get("githubReviewDecision", pr.get("reviewDecision")) == "CHANGES_REQUESTED"
            or not pr.get("reviewsComplete") or not pr.get("reviewRequestsComplete")
            or not pr.get("feedback", {}).get("complete") or not pr.get("threads", {}).get("complete")
            or pr["threads"].get("unresolved") != 0):
        return False
    try:
        verified = datetime.fromisoformat(evidence["verifiedAt"].replace("Z", "+00:00"))
        submitted = datetime.fromisoformat(evidence["submittedAt"].replace("Z", "+00:00"))
        if verified.tzinfo is None or submitted.tzinfo is None or verified < submitted:
            return False
    except (KeyError, AttributeError, ValueError):
        return False
    items = pr["feedback"]["items"]
    approval = next((item for item in items if item.get("kind") == "review"
                     and item.get("id") == evidence.get("reviewId")), None)
    if (not approval or approval.get("state") != "APPROVED"
            or (approval.get("commit") or {}).get("oid") not in {evidence["approvedHead"], evidence["head"]}
            or approval.get("submittedAt") != evidence["submittedAt"]
            or (approval.get("author") or {}).get("login") != evidence.get("reviewer")
            or any(item.get("login") == evidence["reviewer"] for item in pr.get("reviewRequests", []))):
        return False
    for item in items:
        author = (item.get("author") or {}).get("login")
        if author == pr.get("headOwner"):
            continue  # Author announcements neither approve nor revoke a patch.
        value = item.get("submittedAt") or item.get("createdAt")
        try:
            at = datetime.fromisoformat(value.replace("Z", "+00:00"))
            updated = datetime.fromisoformat(item.get("updatedAt", value).replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return False
        if at.tzinfo is None or updated.tzinfo is None:
            return False
        if (item.get("kind") == "review" and item.get("state") == "APPROVED"
                and item.get("id") != evidence["reviewId"] and at > verified
                and (item.get("commit") or {}).get("oid") == pr["head"] and not item.get("body", "").strip()):
            continue  # A new empty approval of this head does not change its patch equivalence.
        if max(at, updated) > verified:
            return False  # New feedback needs owner reconciliation first.
        if (item.get("kind") == "review" and item.get("state") in {"CHANGES_REQUESTED", "DISMISSED"}
                and at >= submitted):
            return False
    return True


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


def landed_patch(pr: dict, proof: dict | None) -> bool:
    """Accept an exact-head, reviewed replay that left the upstream tree intact."""
    if (not proof or pr.get("status") != "open" or pr.get("revisionChanged")
            or proof.get("head") != pr.get("head") or pr.get("branchHead", pr["head"]) not in {None, pr["head"]}
            or proof.get("method") != "merge-tree-identical"
            or not all(isinstance(proof.get(key), str) and re.fullmatch(r"[a-f0-9]{40}", proof[key])
                       for key in ("head", "upstreamHead", "upstreamTree", "replayedTree"))
            or proof["upstreamTree"] != proof["replayedTree"]
            or not isinstance(proof.get("evidence"), list) or not proof["evidence"]
            or not all(isinstance(url, str) and url.startswith("https://") for url in proof["evidence"])):
        return False
    try:
        return datetime.fromisoformat(proof["verifiedAt"].replace("Z", "+00:00")).tzinfo is not None
    except (KeyError, AttributeError, ValueError):
        return False


def import_lineage(pr: dict, patch: dict | None = None) -> list[dict]:
    """An exact-head successor supersedes attempts at a certified earlier patch."""
    current = [item for item in pr.get("imports", [])
               if item.get("matchesSourceHead") and item.get("sourceHead") == pr["head"]]
    if any(item["status"] != "closed" for item in current) or not approved_patch(pr, patch):
        return current
    earlier = [item for item in pr.get("imports", []) if item.get("sourceHead") == patch["approvedHead"]]
    if current:
        return [item for item in earlier if item["status"] == "merged"] or current
    return earlier


def import_trigger(pr: dict) -> dict:
    """Keep review approval separate from the label that starts Copybara."""
    result = {"label": "ready to pull", "state": "unknown"}
    if pr.get("labelsComplete") is not True:
        return result
    if result["label"] in pr.get("labels", []):
        return {**result, "state": "present"}
    history = pr.get("labelHistory", {})
    if history.get("complete") is not True:
        return result
    events = sorted((item for item in history.get("items", [])
                     if (item.get("label") or {}).get("name") == result["label"]),
                    key=lambda item: item["createdAt"])
    if (not events or events[-1].get("__typename") != "UnlabeledEvent"
            or not any(item.get("__typename") == "LabeledEvent" for item in events[:-1])):
        return result
    removed = events[-1]
    return {**result, "state": "missing", "removedAt": removed["createdAt"],
            "actor": (removed.get("actor") or {}).get("login"), "eventId": removed["id"]}


def public_import_disposition(pr: dict, patch: dict | None = None, landing: dict | None = None) -> dict:
    """Share public export progress separately from permission to post reminders."""
    imports = import_lineage(pr, patch)
    accepted = approved_patch(pr, patch)
    active = [item for item in imports if item["status"] in {"open", "draft"}]
    scope = "exact" if imports and imports[0]["sourceHead"] == pr["head"] else "equivalent" if imports else "none"
    approval = effective_approval(pr, require_aggregate=False, trusted_reviewers=
                                 frozenset({patch["reviewer"].casefold()}) if accepted else frozenset())
    binding = {"sourceHead": pr["head"], "approvedHead": patch["approvedHead"] if accepted else None,
               "currentApproval": approval is not None,
               "complete": pr.get("importsComplete") is True,
               "labels": sorted(pr.get("labels", [])), "labelsComplete": pr.get("labelsComplete") is True,
               "labelHistory": pr.get("labelHistory"),
               "imports": [{**{key: item.get(key) for key in ("number", "head", "sourceHead", "status", "mergeable")},
                            "conflicting": item.get("mergeable") == "CONFLICTING" or bool(conflict_qualifier(item))}
                           for item in imports]}
    if active:
        state = "conflicting" if any(item.get("mergeable") == "CONFLICTING" or conflict_qualifier(item)
                                     for item in active) else "open" if any(item["status"] == "open" for item in active) else "draft"
    elif any(item["status"] == "merged" for item in imports):
        state = "merged"
    elif not binding["complete"]:
        state = "unknown"
    else:
        state = "closed" if imports else "absent"
    eligible = (pr.get("importPolicy", "copybara") == "copybara" and pr.get("status") == "open"
                and not pr.get("revisionChanged") and pr.get("branchHead", pr["head"]) in {None, pr["head"]}
                and pr.get("mergeable") == "MERGEABLE" and not landed_patch(pr, landing)
                and (review_decision(pr) == "APPROVED" or accepted or approval
                     and pr.get("githubReviewDecision", pr.get("reviewDecision")) != "CHANGES_REQUESTED"))
    binding["eligible"] = bool(eligible)
    trigger = import_trigger(pr)
    action = None
    if eligible:
        # A later approval without the import label leaves Copybara on the old
        # revision. Do not undo a maintainer's removal made after approval.
        if (binding["complete"] and approval and trigger["state"] == "missing"
                and datetime.fromisoformat(approval["submittedAt"].replace("Z", "+00:00"))
                > datetime.fromisoformat(trigger["removedAt"].replace("Z", "+00:00"))
                and scope != "exact" and state != "closed"
                and not any(item["status"] == "merged" for item in imports)):
            action = "maintainer-import-label"
        elif scope == "equivalent" and active:
            if binding["complete"] and not binding["currentApproval"]:
                action = "maintainer-reapproval"
        elif state == "conflicting":
            action = "maintainer-import-refresh" if scope == "equivalent" else "author-conflicts"
        elif state == "closed":
            action = "maintainer-reimport"
        elif state == "merged":
            action = "maintainer-import-landed"
    description = {
        "conflicting": "Refresh the earlier equivalent export." if scope == "equivalent" else "Resolve the matching export's conflict.",
        "closed": "The export closed without merging; no matching public export is open.",
        "merged": "The export merged; reconcile the source PR.",
        "open": "Waiting for the public export; its checks remain separate.",
        "draft": "The matching public export is a draft.",
        "absent": "No matching public export was found in the complete lookup.",
        "unknown": "Public export inventory is incomplete; refresh before deciding.",
    }[state]
    if action == "maintainer-reapproval":
        description = ("The approval predates the current source head and Copybara still exports the earlier revision. "
                       "Ask the approving maintainer to reapprove the current source PR so Copybara regenerates it.")
    elif action == "maintainer-import-label":
        description = ("The current source has been approved, but its ready to pull label was removed "
                       "and there is no export of this head. Restore the label to start its import.")
    elif scope == "equivalent" and active and binding["currentApproval"]:
        description = "The current source has a fresh approval; wait for Copybara to replace the earlier export."
    elif state == "closed" and trigger["state"] == "missing":
        description = ("The earlier export closed and the previously applied ready to pull label is missing. "
                       "Restore the import trigger; patch approval and the label are separate.")
    elif state == "closed" and trigger["state"] == "present":
        description = "The ready to pull label is present; wait for Copybara to create a replacement export."
        action = None
    return {"binding": binding, "scope": scope, "state": state, "action": action,
            "trigger": trigger, "description": description}


def pr_states(pr: dict, patch: dict | None = None, landing: dict | None = None) -> dict[str, StateEvidence]:
    """Keep source review, public import progress and check provenance distinct."""
    head = pr["head"]
    decision = review_decision(pr)
    accepted_patch = approved_patch(pr, patch)
    if pr["status"] in {"merged", "closed"}:
        timestamp = pr.get("mergedAt" if pr["status"] == "merged" else "closedAt")
        return {pr["status"]: {"scope": head, "transitionAt": timestamp}}
    if landed_patch(pr, landing):
        return {"imported": {"scope": head, "qualifier": "Landed; source PR still open"}}
    states: dict[str, StateEvidence] = {}
    imports = import_lineage(pr, patch)
    active = [item for item in imports if item["status"] in {"open", "draft"}]
    visible = [pr, *(active if active or any(item["status"] == "merged" for item in imports) else imports)]
    failures = [item for item in visible if check_states(item) & FAILURES]
    pending = [item for item in [pr, *active] if check_states(item) & PENDING]
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
    elif decision != "APPROVED" and not accepted_patch:
        states["maintainer-review"] = {"scope": head, "qualifier": "No effective approval for the current head" if pr.get("githubReviewDecision", pr.get("reviewDecision")) == "APPROVED" else None}
    if pr["status"] == "open" and any(is_approval_gate(pr, item) for item in (pr.get("checks") or {}).get("contexts", [])):
        states["maintainer-review"] = {"scope": head, "qualifier": "BuildBuddy requires approving review before these checks run"}
    eligible = (pr["status"] == "open" and (decision == "APPROVED" or accepted_patch)
                and pr.get("branchHead", head) in {None, head})
    if pr.get("importPolicy", "copybara") == "none":
        if eligible and pr.get("mergeable") == "MERGEABLE" and checks_passed(pr):
            states["waiting-merge"] = {"scope": head}
    elif active:
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
    nodes = {node["id"]: node for node in registry["nodes"]}
    published = {pr_identity(pr, registry["meta"]["repo"]): pr for pr in prs}
    states = {identity: pr_states(pr, nodes.get(identity, {}).get("approvedPatch"), nodes.get(identity, {}).get("landedPatch"))
              for identity, pr in published.items()}
    promoted = {(pr["headRepo"], pr["ref"]) for pr in prs
                if pr.get("headRepo") and pr["status"] in {"open", "draft", "merged"}}
    closed = {pr_identity(pr, registry["meta"]["repo"]) for pr in prs if pr["status"] in {"closed", "merged"}}
    for node in registry["nodes"]:
        # Only the task owner records actual pending contributor decisions.
        # An explicit PR amendment keeps its candidate scope separate from the
        # published head, as replies and reviews do from GitHub approval.
        decision = node.get("contributorReview")
        attached = decision and (node["type"] == "pr" and decision.get("kind") == "amendment"
                                 or node["type"] in {"pr", "issue"} and decision.get("kind") in {"reply", "review"})
        if not attached and (node["type"] != "branch" or node["id"] in closed
                             or (node.get("repo"), node.get("ref")) in promoted):
            continue
        pr = published.get(node["id"])
        if decision and decision["status"] == "pending":
            if pr and decision.get("kind") in {"amendment", "review"}:
                if pr["status"] in {"closed", "merged"} or (pr.get("head") and pr["head"] != decision.get("baseHead", decision.get("head"))):
                    continue
            states.setdefault(node["id"], {})["contributor-review"] = {"scope": decision.get("head", decision.get("url"))}
        changes = node.get("contributorChangesRequested")
        if changes and node.get("head") == changes["head"]:
            states.setdefault(node["id"], {})["changes-requested"] = {
                "scope": changes["head"], "qualifier": "Contributor"}
    return states


def record_states(prs: list[dict], registry: dict, previous: dict, checked_at: str) -> ObservationMap:
    current = derive_states(prs, registry)
    current_prs = {pr_identity(pr, registry["meta"]["repo"]): pr for pr in prs}
    previous_prs = {pr_identity(pr, registry["meta"]["repo"]): pr for pr in previous.get("prs", [])}
    prior_prs = {pr_identity(pr, registry["meta"]["repo"]): pr_states(pr) for pr in previous.get("prs", [])}
    # The old collector's aggregate approval cannot backdate the first
    # observation under the exact-head and submission-time rules.
    for pr in previous.get("prs", []):
        if pr.get("reviewDecision") == "APPROVED" and (
                "approvedHead" not in pr or "approvedAt" not in pr):
            prior_prs[pr_identity(pr, registry["meta"]["repo"])].pop("maintainer-review", None)
    history = previous.get("workStates", {})
    result: ObservationMap = {}
    for identity, states in current.items():
        result[identity] = {}
        for state, evidence in states.items():
            if state == "contributor-review":
                observed_at = registry.get("meta", {}).get("updatedAt", checked_at)
            elif pr := current_prs.get(identity):
                observed_at = pr.get("checkedAt", checked_at)
                node = next((node for node in registry["nodes"] if node["id"] == identity), {})
                if state in {"awaiting-import", "waiting-merge"} and approved_patch(pr, node.get("approvedPatch")):
                    observed_at = max(observed_at, node["approvedPatch"]["verifiedAt"],
                                      key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")))
                if state in {"merged", "closed"} and (terminal := pr.get("statusObservation")):
                    observed_at = max(observed_at, terminal["checkedAt"],
                                      key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")))
            else:
                # Contributor decisions are registry evidence, independent of
                # the last complete GitHub collection.
                observed_at = registry.get("meta", {}).get("updatedAt", checked_at)
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
                since, basis = previous_prs[identity].get("checkedAt", previous["checkedAt"]), "observed"
            else:
                since, basis = observed_at, "observed"
            result[identity][state] = {"scope": evidence["scope"], "since": since, "basis": basis,
                                       "qualifier": evidence.get("qualifier"), "observedAt": observed_at}
            if old.get("scope") == evidence["scope"] and old.get("evidence"):
                result[identity][state]["evidence"] = old["evidence"]
    return result
