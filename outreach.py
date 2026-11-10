"""Plan GitHub review requests for idle gVisor source PRs."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from reviewer_selection import History, get_reviewer_candidates, reviewer_history
from work_states import derive_states, effective_approval, landed_patch, maintainer_review_history, matches_feedback_assignment

PACIFIC = ZoneInfo("America/Los_Angeles")
MARKER = re.compile(r"<!-- gvisor-review-outreach (\d+):([0-9a-f]{40}):([A-Za-z0-9-]+) -->")
MENTION = re.compile(r"(?<![\w/@])@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)\b")
ASK = re.compile(r"\b(ptal|re-?review|re-?approve|take (?:another |a )?look|(?:please|could|would|can|available to|able to|time to)\b[^\n.?!]{0,65}?\b(?:review|approve|look))\b", re.I)


def instant(value: str | datetime) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


def business_deadline(start: str | datetime, days: int) -> datetime:
    local = instant(start).astimezone(PACIFIC)
    while days:
        local += timedelta(days=1)
        if local.weekday() < 5:
            days -= 1
    return local


def requested_in_comment(item: dict, number: int) -> set[str]:
    """Count visible requests by the PR author, including hand-written ones."""
    body = item.get("body", "")
    marker = MARKER.search(body)
    if marker and int(marker[1]) == number:
        return {marker[3].lower()}
    # Quoted reviews, code examples and sign-offs don't contact a reviewer.
    text = re.sub(r"\x60\x60\x60.*?(?:\x60\x60\x60|$)", "", body, flags=re.S)
    names = set()
    for line in text.splitlines():
        if not line.lstrip().startswith(">") and ASK.search(line):
            names.update(name.lower() for name in MENTION.findall(line))
    return names


def outreach_states(snapshot: dict, registry: dict) -> dict[str, dict]:
    """Resolve current source work and the registry's explicit prerequisites."""
    prs = snapshot["prs"]
    states = derive_states(prs, registry)
    nodes = {n["id"]: n for n in registry["nodes"]}
    observed = {f"pr:{p['number']}": p for p in prs if p.get("repo") == "google/gvisor"}
    by_url = {p["url"]: p for p in prs if p.get("url")}

    def landed(identity: str) -> bool:
        n = nodes.get(identity, {})
        if (p := observed.get(identity) or by_url.get(n.get("url"))):
            return p["status"] == "merged" or landed_patch(p, n.get("landedPatch"))
        if n.get("status", "").lower() in {"merged", "landed", "released"}:
            return True
        links = n.get("followup", {}).get("evidence", [])
        return any(by_url.get(url, {}).get("status") == "merged" for url in links)

    for e in registry.get("edges", []):
        if e["type"] == "depends_on" and e["to"] in observed and not landed(e["from"]):
            states.setdefault(e["to"], {})["prerequisite"] = {"scope": e["from"]}
    return {str(p["number"]): states.get(identity, {}) for identity, p in observed.items()}


def author_work(pr: dict, node: dict, owner: str) -> bool:
    feedback = pr["feedback"]["items"]
    assignment = node.get("followup", {}).get("actionableComment", {})
    if any(matches_feedback_assignment(pr, row, assignment) for row in feedback):
        return True
    threads = {}
    for row in sorted(feedback, key=lambda r: r.get("createdAt") or ""):
        if row.get("kind") == "inline":
            threads[row["threadId"]] = row
    open_threads = [r for r in threads.values() if not r.get("isResolved")]
    if len(open_threads) != pr["threads"]["unresolved"] or any(
        r.get("author", {}).get("login") != owner for r in open_threads
    ):
        return True
    if pr.get("githubReviewDecision") != "CHANGES_REQUESTED":
        return False
    reviews = [r for r in feedback if r["kind"] == "review" and r.get("state") == "CHANGES_REQUESTED"]
    # A new head or explicit answer on the reviewer's thread can be ready
    # for re-review. An outstanding whole-review request still needs a fix.
    return not reviews or any(
        (r.get("commit") or {}).get("oid", pr["head"]) == pr["head"] and not
        any(t.get("author", {}).get("login") == r.get("author", {}).get("login")
            and t.get("kind") == "inline" and (t.get("isResolved") or
                threads[t["threadId"]].get("author", {}).get("login") == owner) for t in feedback)
        for r in reviews
    )


def plan(pr: dict, node: dict, states: dict, now: str | datetime, owner: str, maintainers: set[str],
         history: History, *, planned: dict[str, int] | None = None, preferred: str | None = None) -> dict | None:
    """Decide the next request from the same observations the sender verifies."""
    now = instant(now)
    if (pr.get("repo") != "google/gvisor" or pr.get("authorLogin") != owner
            or pr.get("status") != "open" or pr.get("headOwner") != owner):
        return None
    number, head = pr["number"], pr["head"]
    blocked = {"prerequisite", "conflicts", "draft", "contributor-review", "failing",
               "checks-pending", "imported", "importing", "awaiting-import", "waiting-merge"}
    if (states.keys() & blocked or pr.get("mergeable") != "MERGEABLE"
            or pr.get("branchHead") != head or node.get("reviewOutreachHold")
            or node.get("releaseWait", {}).get("head") == head
            or node.get("reapproval", {}).get("head") == head
            or node.get("contributorChangesRequested", {}).get("head") == head
            or effective_approval(pr, require_aggregate=False, trusted_reviewers=set(pr.get("maintainerReviewers", [])) | set(maintainers))
            or landed_patch(pr, node.get("landedPatch"))
            or (pr.get("publicImportDisposition") or {}).get("reviewedExport")
            or (pr.get("publicImportDisposition") or {}).get("action") == "maintainer-reapproval"):
        return None
    if not (pr.get("headHistoryComplete") and pr.get("reviewsComplete") and pr.get("reviewRequestsComplete")
            and pr.get("feedback", {}).get("complete") and pr.get("importsComplete")
            and pr.get("reviewDismissals", {}).get("complete")
            and pr.get("checks", {}).get("complete") and pr.get("threads", {}).get("complete")
            and pr.get("reviewTimeline", {}).get("complete") and pr.get("files")):
        return {"number": number, "head": head, "status": "incomplete"}
    # The existing import/reapproval alarm owns earlier formal approvals,
    # including reviews GitHub dismissed after a push.
    decisions = maintainer_review_history(pr)
    if decisions is None or decisions[1] or author_work(pr, node, owner):
        return None
    timeline = pr["reviewTimeline"]["items"]
    source = [pr.get("headCommittedAt"), pr.get("headIntroducedAt")]
    proof = node.get("outreachReviewClock", {})
    if (proof.get("head") == head and proof.get("method") == "reviewed-context-only-rebase"
            and pr.get("headRebasedFrom") and proof.get("priorHead") == pr["headRebasedFrom"]
            and re.fullmatch(r"[0-9a-f]{64}", proof.get("changedLinesSha256", ""))
            and proof.get("evidence") and proof.get("reviewableSince")):
        source = [proof["reviewableSince"]]
    started = max(instant(t) for t in [pr["createdAt"], *source,
                  *(e["at"] for e in timeline if e["kind"] in {"ready", "reopen"})] if t)
    activity = started
    owner_activity = started
    contacts = {}
    current = {r["login"].lower() for r in pr["reviewRequests"] if r.get("login")}
    responses = {}
    automatic = set()
    removed = set()
    codeowners = {name.lower() for name in pr.get("codeOwnerRequests", [])}
    seen_requests = set()
    opening = instant(pr["createdAt"])
    for event in timeline:
        reviewer = (event.get("reviewer") or {}).get("login", "").lower()
        if not reviewer:
            continue
        if event["kind"] == "remove" and instant(event["at"]) >= started:
            removed.add(reviewer)
        if event["kind"] != "request":
            continue
        asked = instant(event["at"])
        # GitHub can attribute its initial CODEOWNERS request to the PR
        # author. A later explicit request still starts the personal deadline.
        opening_codeowner = (reviewer in codeowners and reviewer not in seen_requests
                             and opening <= asked <= opening + timedelta(minutes=1))
        seen_requests.add(reviewer)
        if (event.get("actor") or {}).get("__typename") == "Bot" or opening_codeowner:
            automatic.add(reviewer)
        else:
            contacts[reviewer] = max(contacts.get(reviewer, instant(event["at"])), instant(event["at"]))
    for item in pr["feedback"]["items"]:
        who = item.get("author") or {}
        if who.get("__typename") != "User" or who.get("login", "").lower() in {"copybara-service","googlebot"}:
            continue
        at = instant(item.get("updatedAt") or item.get("submittedAt") or item.get("createdAt"))
        if not at:
            continue
        login = who["login"].lower()
        asks = requested_in_comment(item, number) if login == owner.lower() and item["kind"] in {"comment","inline"} else set()
        if asks:
            asked = instant(item.get("createdAt")) or at
            for name in asks:
                contacts[name] = max(contacts.get(name, asked), asked)
            continue
        if item.get("state") == "DISMISSED" or (login != owner.lower() and item.get("body", "").lstrip().startswith("[🤖")):
            continue
        activity = max(activity, at)
        if login == owner.lower():
            owner_activity = max(owner_activity, at)
        else:
            responses[login] = max(responses.get(login, at), at)
    first_due = business_deadline(activity, 5)
    prior = {name for name, at in contacts.items() if not (
        name in responses and at < responses[name] < owner_activity)}
    times = [at for name, at in contacts.items() if name in prior and at >= started]
    due = max(first_due, business_deadline(max(times), 2)) if times else first_due
    info = {"number": number, "head": head, "lastActivity": activity.isoformat(), "dueAt": due.isoformat()}
    if times:
        info["lastContactAt"] = max(times).isoformat()
        info["lastReviewers"] = sorted(name for name in prior if contacts[name] == max(times))
    if current - contacts.keys() - automatic:
        return {**info, "status": "incomplete"}
    if now < due or now.astimezone(PACIFIC).weekday() >= 5:
        return {**info, "status": "waiting"}
    # A reviewer who responded on a previous revision is eligible again after
    # new source lands. An unanswered or still-pending request is not repeated.
    excluded = prior | removed
    manual_current = current & prior
    candidates = get_reviewer_candidates(pr, history, excluded | manual_current, current, now, planned=planned)
    candidates = [c for c in candidates if c["login"].lower() not in excluded | manual_current
                  and (c["login"].lower() not in current or c["channel"] == "comment")
                  and (preferred is None or c["login"].lower() == preferred.lower())]
    if not candidates:
        return {**info, "status": "needs-reviewer"}
    candidate = candidates[0]
    who = candidate["login"]
    channel = candidate["channel"]
    scope = candidate["scope"]
    key = f"{number}:{head}:{who.lower()}"
    return {**info, "status": "due", "key": key, "reviewer": who,
            "channel": "request" if channel == "review_request" else channel,
            "evidence": candidate["evidence"], "reason": candidate["reason"],
            "body": f"@{who}, could you review the changes to `{scope}` in this PR? {candidate['reason']}\n\n"
                    f"Posted by Codex on behalf of @{owner}.\n\n<!-- gvisor-review-outreach {key} -->"}

