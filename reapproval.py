"""Plan revision-bound reminders from reviewed amendments and GitHub history."""

from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

from work_states import approved_patch, effective_approval, landed_patch, public_import_disposition, with_maintainer_reviewers


MARKER = "<!-- codex-reapproval-reminder:v1 "
PACIFIC = ZoneInfo("America/Los_Angeles")
REMINDER_INTERVAL = timedelta(hours=3)


def maintainer_logins(text: str) -> frozenset[str]:
    """Read current maintainers from the repository's generated public roster."""
    current, separator, _ = text.partition("\n## Emeritus Maintainers\n")
    if (not text.startswith("<!-- Generated from governance/maintainers.yaml by\n")
            or not separator):
        raise ValueError("Canonical maintainer roster format is missing")
    rows = [line for line in current.splitlines() if "|" in line]
    if len(rows) < 3 or not rows[0].startswith("Name ") or set(rows[1]) - set("-| "):
        raise ValueError("Canonical current-maintainer table is missing")
    logins = []
    for row in rows[2:]:
        match = re.fullmatch(r"[^|]+\|\s*\[@([A-Za-z0-9-]+)\]\(https://github\.com/([A-Za-z0-9-]+)\)\s*\|[^|]+", row)
        if not match or match[1].casefold() != match[2].casefold():
            raise ValueError("Malformed canonical maintainer row")
        logins.append(match[1].casefold())
    if len(logins) != len(set(logins)):
        raise ValueError("Duplicate canonical maintainer identity")
    return frozenset(logins)


def import_author_block(pr: dict, node: dict) -> str | None:
    """Leave concrete current-source work with the author before import recovery."""
    if pr.get("threads", {}).get("complete") is not True or pr["threads"].get("unresolved"):
        return "unresolved-or-incomplete-author-threads"
    comment = node.get("followup", {}).get("actionableComment", {})
    if (comment.get("head") == pr["head"] and any(item.get("kind") == "comment"
            and item.get("id") == comment.get("commentId") and item.get("url") == comment.get("url")
            for item in pr.get("feedback", {}).get("items", []))):
        return "registered-current-author-action"
    if node.get("releaseWait", {}).get("head") == pr["head"]:
        return "registered-current-release-wait"
    for item in pr.get("feedback", {}).get("items", []):
        if (item.get("kind") != "comment" or item.get("body", "").strip() != "[🤖] Needs rebase."
                or (item.get("author") or {}).get("login") != "EtiennePerot"):
            continue
        introduced = pr.get("headIntroducedAt")
        if (not pr.get("headHistoryComplete") or not introduced
                or datetime.fromisoformat(item["updatedAt"].replace("Z", "+00:00"))
                >= datetime.fromisoformat(introduced.replace("Z", "+00:00"))):
            return "rebase-request-not-yet-followed-by-source-update"
    return None


def reminder_plan(pr: dict, node: dict, now: datetime, owner: str,
                  maintainers: frozenset[str]) -> dict:
    """Plan either an exact amendment review or a verified missing import trigger."""
    pr = with_maintainer_reviewers(pr, maintainers)
    result = {"repo": pr.get("repo"), "number": pr["number"], "head": pr.get("head"),
              "publicImportDisposition": public_import_disposition(pr, node.get("approvedPatch"), node.get("landedPatch"))}

    def skip(reason: str) -> dict:
        return {**result, "status": "skipped", "reason": reason}

    if now.tzinfo is None:
        raise ValueError("Reminder time must include its timezone")
    if (pr.get("repo") != "google/gvisor" or pr.get("importPolicy") != "copybara"
            or pr.get("authorLogin") != owner or pr.get("status") != "open"):
        return skip("not-an-open-owned-copybara-source")
    if pr.get("branchHead") and pr["branchHead"] != pr["head"]:
        return {**result, "status": "deferred", "reason": "source-branch-differs-from-pr"}
    if landed_patch(pr, node.get("landedPatch")):
        return {**skip("patch-already-on-master"), "evidence": node["landedPatch"]["evidence"]}
    if pr.get("mergeable") != "MERGEABLE":
        return {**result, "status": "deferred", "mergeable": pr.get("mergeable"),
                "reason": "source-conflicted" if pr.get("mergeable") == "CONFLICTING" else "mergeability-unknown"}
    if not pr.get("reviewsComplete") or not pr.get("reviewDismissals", {}).get("complete"):
        return skip("incomplete-review-or-comment-history")
    comments = [item for item in pr.get("feedback", {}).get("items", []) if item.get("kind") == "comment"]
    if not pr.get("commentsComplete"):
        try:
            oldest = min(datetime.fromisoformat(item["createdAt"].replace("Z", "+00:00")) for item in comments)
        except (KeyError, AttributeError, ValueError):
            return skip("incomplete-reminder-window")
        if oldest.tzinfo is None or now - oldest < REMINDER_INTERVAL:
            return skip("incomplete-reminder-window")
    dismissed = {(event.get("review") or {}).get("id"): event["previousReviewState"]
                 for event in pr["reviewDismissals"]["items"]}
    reviews = [item for item in pr["feedback"]["items"] if item.get("kind") == "review"
               and (item.get("author") or {}).get("__typename") == "User"
               and (item.get("author") or {}).get("login") not in {None, owner}
               and (item.get("authorAssociation") in {"MEMBER", "OWNER", "COLLABORATOR"}
                    or item["author"]["login"].casefold() in maintainers)]
    latest = {}
    for item in reviews:
        if item.get("state") not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        author = item["author"]["login"]
        if not item.get("submittedAt"):
            return skip("missing-review-time")
        if author not in latest or item["submittedAt"] > latest[author]["submittedAt"]:
            latest[author] = item
    disposition = result["publicImportDisposition"]
    if (disposition["binding"]["eligible"] and disposition["state"] == "closed"
            and disposition["trigger"]["state"] == "present"):
        return skip("import-trigger-present-awaiting-replacement")
    if disposition["action"] == "maintainer-reapproval":
        if effective_approval(pr, require_aggregate=False, trusted_reviewers=maintainers):
            return skip("current-head-approved")
        if reason := import_author_block(pr, node):
            return {**skip(reason), "status": "deferred"}
        patch = node["approvedPatch"]
        approvers = sorted(item["author"]["login"] for item in latest.values()
                           if item["id"] == patch["reviewId"] and item["state"] == "APPROVED")
        if not approvers:
            return skip("no-matching-prior-maintainer-approval")
        if not pr.get("headHistoryComplete") or not pr.get("headIntroducedAt"):
            return {**skip("incomplete-head-time-evidence"), "status": "needs-evidence"}
        if datetime.fromisoformat(patch["submittedAt"].replace("Z", "+00:00")) >= datetime.fromisoformat(pr["headIntroducedAt"].replace("Z", "+00:00")):
            return {**skip("approval-does-not-predate-source-update"), "status": "needs-evidence"}
        head, old = pr["head"], patch["approvedHead"]
        exports = ", ".join(f"[#{item['number']}](https://github.com/google/gvisor/pull/{item['number']})"
                            for item in disposition["binding"]["imports"] if item["status"] in {"open", "draft"})
        body = (f"🚨 {' '.join('@' + login for login in approvers)} please reapprove the current source head "
                f"[{head[:12]}](https://github.com/google/gvisor/commit/{head}) so Copybara can regenerate its import. "
                f"Your earlier approval covered [{old[:12]}](https://github.com/google/gvisor/commit/{old}) "
                f"and was submitted {patch['submittedAt']}, before this head was published {pr['headIntroducedAt']}. "
                f"The open Copybara PR {exports} still names the earlier source revision. "
                "The source rebase preserves the reviewed patch, but that comparison and the older export "
                "do not establish approval of the current head.\n\n"
                f"Posted by Codex on behalf of @{owner}.\n\n{MARKER}head={head} -->")
        return schedule_reminder(result, comments, now, owner, action="reapprove-amendment",
                                 approvedHead=old, approvers=approvers, body=body,
                                 importReapproval=True)
    if disposition["action"] in {"maintainer-import-label", "maintainer-reimport"} and disposition["trigger"]["state"] == "missing":
        if reason := import_author_block(pr, node):
            return {**skip(reason), "status": "deferred"}
        current_approval = effective_approval(pr, require_aggregate=False, trusted_reviewers=maintainers)
        patch = node.get("approvedPatch", {})
        approval = current_approval
        if not approval and approved_patch(pr, patch):
            approval = latest.get(patch["reviewer"])
            if approval and approval["id"] != patch["reviewId"]:
                approval = None
        if not approval or approval["state"] != "APPROVED":
            return skip("no-qualified-import-approver")
        approvers = [approval["author"]["login"]]
        trigger = disposition["trigger"]
        imports = disposition["binding"]["imports"]
        if disposition["state"] != "closed":
            imports = [item for item in imports if item["status"] in {"open", "draft"}]
        exports = ", ".join(f"[#{item['number']}](https://github.com/google/gvisor/pull/{item['number']})" for item in imports)
        if disposition["state"] == "closed":
            export_status = f"The export ({exports}) closed without merging."
        elif exports:
            export_status = f"Copybara still exports an earlier revision ({exports})."
        else:
            export_status = "There is no public export for this head."
        head = pr["head"]
        body = (f"🚨 {' '.join('@' + login for login in approvers)} please restore `ready to pull` "
                f"to import [{head[:12]}](https://github.com/google/gvisor/commit/{head}). "
                f"{'You already approved this head.' if current_approval else 'The reviewed patch is unchanged.'} "
                f"{export_status}\n\n"
                f"Posted by Codex on behalf of @{owner}.\n\n{MARKER}head={head} action=restore-import-label -->")
        return schedule_reminder(result, comments, now, owner, action="restore-import-label",
                                 approvers=approvers, removedAt=trigger["removedAt"], body=body)
    current = [item for item in latest.values() if (item.get("commit") or {}).get("oid") == pr["head"]]
    for item in current:
        if item["state"] != "APPROVED":
            continue
        if not pr.get("headHistoryComplete") or not pr.get("headCommittedAt"):
            return skip("incomplete-head-time-evidence")
        try:
            cutoff = max(datetime.fromisoformat(value.replace("Z", "+00:00"))
                         for value in (pr["headCommittedAt"], pr.get("headIntroducedAt")) if value)
            submitted = datetime.fromisoformat(item["submittedAt"].replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return skip("incomplete-head-time-evidence")
        if cutoff.tzinfo is None or submitted.tzinfo is None:
            return skip("incomplete-head-time-evidence")
        if submitted >= cutoff:
            return skip("current-head-approved")
    if any(item["state"] == "CHANGES_REQUESTED" for item in current):
        return skip("current-head-needs-source-work")
    prior_approvals = [item for item in latest.values()
                       if item["state"] == "APPROVED" or item["state"] == "DISMISSED"
                       and dismissed.get(item["id"]) == "APPROVED"]
    if not prior_approvals:
        return skip("no-prior-approval")
    patch = node.get("approvedPatch", {})
    if patch.get("head") == pr["head"]:
        if not approved_patch(pr, patch):
            return {**skip("unconfirmed-patch-equivalence"), "status": "needs-evidence"}
        if pr.get("importsComplete"):
            for item in pr.get("imports", []):
                if (item["status"] == "open" and
                        item.get("sourceHead") in {patch["approvedHead"], pr["head"]}):
                    return {**skip("open-export-of-approved-patch"), "export": item["url"]}
        return skip("reviewed-patch-preserving-rebase")
    proof = node.get("reapproval", {})
    match proof:
        case {"head": str(head), "approvedHead": str(old),
              "kind": ("content-amendment" | "patch-preserving-rebase") as kind,
              "method": "reviewed-source-diff", "diffSha256": str(digest),
              "summary": str(summary), "evidence": str(evidence)}:
            if (head != pr["head"] or head == old or not re.fullmatch(r"[0-9a-f]{40}", head)
                    or not re.fullmatch(r"[0-9a-f]{40}", old) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or not summary.strip() or not evidence.startswith("https://github.com/")):
                return {**skip("invalid-amendment-evidence"), "status": "needs-evidence"}
        case _:
            return {**skip("missing-exact-content-amendment-evidence"), "status": "needs-evidence"}
    if kind == "patch-preserving-rebase":
        return skip("reviewed-patch-preserving-rebase")
    approvers = sorted(item["author"]["login"] for item in prior_approvals
                       if (item.get("commit") or {}).get("oid") == old)
    if not approvers:
        return skip("no-matching-prior-maintainer-approval")
    commit_url = "https://github.com/google/gvisor/commit/"
    body = (f"🚨 {' '.join('@' + login for login in approvers)} please reapprove the amended head "
            f"[{head[:12]}]({commit_url}{head}) before it is imported. Your earlier approval covered "
            f"[{old[:12]}]({commit_url}{old}). Copybara may still import the earlier approved "
            f"revision and omit this amendment.\n\n{summary}\n\n"
            f"[Amendment evidence]({evidence})\n\n"
            f"Posted by Codex on behalf of @{owner}.\n\n{MARKER}head={head} -->")
    return schedule_reminder(result, comments, now, owner, approvedHead=old,
                             approvers=approvers, diffSha256=digest, body=body,
                             action="reapprove-amendment")


def schedule_reminder(result: dict, comments: list[dict], now: datetime, owner: str, **details: object) -> dict:
    """Share the per-PR cooldown and Pacific-hours gate across reminder purposes."""
    for item in comments:
        if ((item.get("author") or {}).get("login") != owner
                or MARKER not in item.get("body", "")):
            continue
        try:
            sent = datetime.fromisoformat(item["createdAt"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, ValueError):
            return {**result, "status": "skipped", "reason": "unreadable-prior-reminder-time"}
        if sent.tzinfo is None or now - sent < REMINDER_INTERVAL:
            return {**result, "status": "skipped", "reason": "reminded-within-three-hours"}
    planned = {**result, **details, "status": "due"}
    local = now.astimezone(PACIFIC)
    if local.weekday() >= 5 or not 9 <= local.hour < 17:
        planned.update(status="deferred", reason="outside-pacific-business-hours")
    return planned
