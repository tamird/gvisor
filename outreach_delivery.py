"""Deliver one review request to its selected reviewer after refreshing the PR."""

import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = "google/gvisor"
FORK = "tamird/gvisor"
ROOT = Path(__file__).resolve().parent
PACIFIC = ZoneInfo("America/Los_Angeles")


class GitHubError(RuntimeError):
    def __init__(self, status: int | None, message: str, limited: bool = False):
        super().__init__(message)
        self.status = status
        self.limited = limited


def api(path: str, data: dict | None = None) -> dict:
    command = ["gh", "api", "--hostname", "github.com", "--include"]
    if data is not None:
        command.extend(["--method", "POST", "--input", "-"])
    try:
        result = subprocess.run([*command, path], input=json.dumps(data) if data is not None else None,
                                text=True, capture_output=True, timeout=35)
    except subprocess.TimeoutExpired as error:
        raise GitHubError(None, "GitHub request timed out") from error
    headers, _, raw = result.stdout.partition("\n\n")
    first, *lines = headers.splitlines() or [""]
    match = re.match(r"HTTP/\S+ (\d+)", first)
    status = int(match[1]) if match else None
    limits = {key.lower(): value.strip() for line in lines if ":" in line
              for key, value in [line.split(":", 1)]}
    limited = status == 429 or "retry-after" in limits or limits.get("x-ratelimit-remaining") == "0"
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = None
    if not isinstance(body, dict):
        # A proxy's error page does not establish that GitHub rejected a write.
        raise GitHubError(None, "No readable GitHub response", limited)
    if result.returncode or status is None or not 200 <= status < 300:
        message = str(body.get("message") or result.stderr or "No GitHub response")[:400]
        limited = limited or any(s in (message + str(body.get("errors", ""))).lower()
                                 for s in ("rate limit", "abuse", "spam"))
        raise GitHubError(status if body.get("message") else None, message, limited)
    return body


def registry_is_published() -> bool:
    local = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD:registry.json"],
                                    text=True, timeout=15).strip()
    return api(f"repos/{FORK}/contents/registry.json?ref=gh-pages").get("sha") == local


def matches(scheduled: dict, fresh: dict | None) -> bool:
    return bool(fresh and fresh.get("status") == "due" and all(
        fresh.get(key) == scheduled[key] for key in ("number", "head", "reviewer", "channel", "key", "body")))


def send_one(action: dict, refresh: Callable[[], dict | None], owner: str) -> dict:
    number = action["number"]
    reviewer = action["reviewer"]
    marker = f"<!-- gvisor-review-outreach {action['key']} -->"
    if (not isinstance(number, int) or number < 1 or not re.fullmatch(r"[0-9a-f]{40}", action["head"])
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,38}", reviewer) or reviewer.lower() == owner.lower()
            or action["channel"] not in {"request", "comment"} or not action["body"].endswith(marker)
            or f"@{reviewer}" not in action["body"]):
        raise ValueError("Invalid review-outreach destination or body")
    if not matches(action, refresh()):
        return {"status": "superseded"}
    if not registry_is_published():
        return {"status": "registry-changed"}

    requests = f"repos/{REPO}/pulls/{number}/requested_reviewers"
    if action["channel"] == "request":
        def pending() -> bool:
            observed = api(requests + "?per_page=100")
            return any(row["login"].lower() == reviewer.lower() for row in observed["users"])

        try:
            api(requests, {"reviewers": [reviewer]})
        except GitHubError as error:
            if error.limited:
                raise
            # A timeout or 5xx can follow a successful write.
            if pending():
                return {"status": "request-observed", "reviewer": reviewer}
            if error.status not in (403, 404, 422):
                raise
            # A definite rejection can mean the PR author lacks triage access.
            # Re-read the PR and the registry before falling back to a mention.
            if not matches(action, refresh()):
                return {"status": "superseded"}
            if not registry_is_published():
                return {"status": "registry-changed"}
        else:
            if not pending():
                raise GitHubError(None, "Review request was acknowledged but is no longer pending")
            return {"status": "requested", "reviewer": reviewer}

    comments = f"repos/{REPO}/issues/{number}/comments"
    posted = api(comments, {"body": action["body"]})
    comment_id = posted.get("id")
    if type(comment_id) is not int:
        raise GitHubError(None, "Cannot identify the submitted review reminder")
    saved = api(f"repos/{REPO}/issues/comments/{comment_id}")
    if (saved.get("id") != comment_id or saved.get("user", {}).get("login") != owner
            or saved.get("body") != action["body"] or saved.get("issue_url") != f"https://api.github.com/repos/{REPO}/issues/{number}"):
        raise GitHubError(None, f"Could not verify the posted comment on #{number}")
    return {"status": "commented", "url": saved["html_url"], "fallback": action["channel"] == "request"}


def deliver(snapshot: dict, registry: dict, now: str | datetime, owner: str, maintainers: set[str],
            fetch_fresh: Callable[[int], dict], *, post: bool) -> None:
    from outreach import outreach_states, plan, reviewer_history

    if owner != "tamird" or registry["meta"]["repo"] != REPO:
        raise ValueError("Review outreach is configured only for tamird's gVisor PRs")
    plans = sorted((a for a in snapshot.get("reviewOutreach", {}).values() if a.get("status") == "due"),
                   key=lambda a: (a["dueAt"], a["number"]))
    if not plans:
        return
    if post and api("user").get("login") != owner:
        raise RuntimeError("Review-outreach credential does not belong to the configured owner")
    nodes = {node["number"]: node for node in registry["nodes"] if node.get("type") == "pr" and node.get("repo") == REPO}
    specialties = {name: set(areas) for name, areas in snapshot.get("reviewerSpecialties", {}).items()}
    history = reviewer_history(snapshot["prs"], owner, maintainers=maintainers, specialties=specialties,
                               observations=snapshot.get("reviewerActivity", {}).get("observations", []))
    # Leave time for the final GitHub readback before the workflow ends.
    deadline = time.monotonic() + 12 * 60 if os.environ.get("WORK_MAP_SCHEDULED_REVIEW_OUTREACH") == "true" else None
    for index, scheduled in enumerate(plans):
        if deadline is not None and time.monotonic() >= deadline:
            print(json.dumps({"reviewOutreach": "deferred", "remaining": len(plans) - index,
                              "nextPR": scheduled["number"], "reason": "workflow time budget"}), flush=True)
            return
        number = scheduled["number"]

        def refresh() -> dict | None:
            pr = fetch_fresh(number)
            if (not pr or pr.get("number") != number or pr.get("repo") != REPO or pr.get("authorLogin") != owner
                    or pr.get("status") != "open" or pr.get("head") != scheduled["head"]):
                return None
            latest = {**snapshot, "prs": [pr if (p.get("repo"), p.get("number")) == (REPO, number) else p
                                          for p in snapshot["prs"]]}
            at = datetime.now(timezone.utc) if post else now
            instant = datetime.fromisoformat(at.replace("Z", "+00:00")) if isinstance(at, str) else at
            if instant.astimezone(PACIFIC).weekday() >= 5:
                return None
            return plan(pr, nodes.get(number, {}), outreach_states(latest, registry).get(str(number), {}),
                        at, owner, maintainers, history, preferred=scheduled["reviewer"])

        if not post:
            result = {"status": "would-send" if matches(scheduled, refresh()) else "superseded"}
        else:
            try:
                result = send_one(scheduled, refresh, owner)
            except GitHubError as error:
                print(json.dumps({"reviewOutreach": number, "status": "unconfirmed", "http": error.status,
                                  "error": str(error), "rateLimited": error.limited}), flush=True)
                # There is no write retry. The next pass starts from the public timeline.
                if error.limited or error.status in (None, 401):
                    return
                continue
        print(json.dumps({"reviewOutreach": number, "key": scheduled["key"], **result}), flush=True)
        if result["status"] == "registry-changed":
            return
