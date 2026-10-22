"""Plan revision-bound reminders from reviewed amendments and GitHub history."""

from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo


MARKER = "<!-- codex-reapproval-reminder:v1 "
PACIFIC = ZoneInfo("America/Los_Angeles")


def reminder_plan(pr: dict, node: dict, now: datetime, owner: str) -> dict:
    """Fail closed unless an exact reviewed amendment still needs approval."""
    result = {"repo": pr.get("repo"), "number": pr["number"], "head": pr.get("head")}

    def skip(reason: str) -> dict:
        return {**result, "status": "skipped", "reason": reason}

    if now.tzinfo is None:
        raise ValueError("Reminder time must include its timezone")
    if (pr.get("repo") != "google/gvisor" or pr.get("importPolicy") != "copybara"
            or pr.get("authorLogin") != owner or pr.get("status") != "open"):
        return skip("not-an-open-owned-copybara-source")
    if not pr.get("reviewsComplete") or not pr.get("reviewDismissals", {}).get("complete"):
        return skip("incomplete-review-or-comment-history")
    comments = [item for item in pr.get("feedback", {}).get("items", []) if item.get("kind") == "comment"]
    if not pr.get("commentsComplete"):
        try:
            oldest = min(datetime.fromisoformat(item["createdAt"].replace("Z", "+00:00")) for item in comments)
        except (KeyError, AttributeError, ValueError):
            return skip("incomplete-reminder-window")
        if oldest.tzinfo is None or now - oldest < timedelta(hours=1):
            return skip("incomplete-reminder-window")
    dismissed = {(event.get("review") or {}).get("id"): event["previousReviewState"]
                 for event in pr["reviewDismissals"]["items"]}
    reviews = [item for item in pr["feedback"]["items"] if item.get("kind") == "review"
               and (item.get("author") or {}).get("__typename") == "User"
               and (item.get("author") or {}).get("login") not in {None, owner}
               and item.get("authorAssociation") in {"MEMBER", "OWNER", "COLLABORATOR"}]
    latest = {}
    for item in reviews:
        if item.get("state") not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        author = item["author"]["login"]
        if not item.get("submittedAt"):
            return skip("missing-review-time")
        if author not in latest or item["submittedAt"] > latest[author]["submittedAt"]:
            latest[author] = item
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
    if node.get("approvedPatch", {}).get("head") == pr["head"]:
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
                       if (item.get("commit") or {}).get("oid") == old
                       and item.get("authorAssociation") in {"MEMBER", "OWNER", "COLLABORATOR"})
    if not approvers:
        return skip("no-matching-prior-maintainer-approval")
    for item in comments:
        if ((item.get("author") or {}).get("login") != owner
                or MARKER not in item.get("body", "")):
            continue
        try:
            sent = datetime.fromisoformat(item["createdAt"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, ValueError):
            return skip("unreadable-prior-reminder-time")
        if sent.tzinfo is None or now - sent < timedelta(hours=1):
            return skip("reminded-within-one-hour")
    commit_url = "https://github.com/google/gvisor/commit/"
    body = (f"🚨 {' '.join('@' + login for login in approvers)} please reapprove the amended head "
            f"[{head[:12]}]({commit_url}{head}) before it is imported. Your earlier approval covered "
            f"[{old[:12]}]({commit_url}{old}). Copybara may still import the earlier approved "
            f"revision and omit this amendment.\n\n{summary}\n\n"
            f"[Amendment evidence]({evidence})\n\n"
            f"Posted by Codex on behalf of @{owner}.\n\n{MARKER}head={head} -->")
    planned = {**result, "status": "due", "approvedHead": old, "approvers": approvers,
               "diffSha256": digest, "body": body}
    local = now.astimezone(PACIFIC)
    if local.weekday() >= 5 or not 9 <= local.hour < 17:
        planned.update(status="deferred", reason="outside-pacific-business-hours")
    return planned
