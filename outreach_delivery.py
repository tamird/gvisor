"""Deliver one review request to its selected reviewer after refreshing the PR."""

import base64
import hashlib
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
PACIFIC = ZoneInfo("America/Los_Angeles")


class GitHubError(RuntimeError):
    def __init__(self, status: int | None, message: str, limited: bool = False):
        super().__init__(message)
        self.status = status
        self.limited = limited


def posting_mode(paused: str | None, rotations: str | None) -> str:
    if paused == "false":
        return "all"
    if paused == "true":
        return "rotations" if rotations == "true" else "paused"
    return "unknown"


def live_posting_mode() -> str:
    # Scope output to the two switches. The workflow snapshot and caller
    # environment can be stale by the time a reviewer is contacted.
    query = ('.variables[] | select(.name == "WORK_MAP_POSTING_PAUSED" or '
             '.name == "WORK_MAP_REVIEW_ROTATIONS_ENABLED") | [.name,.value] | @tsv')
    result = subprocess.run(["gh", "api", "--hostname", "github.com", "--paginate",
                             f"repos/{FORK}/actions/variables?per_page=100", "--jq", query],
                            text=True, capture_output=True, check=True, timeout=30)
    switches = dict(line.split("\t") for line in result.stdout.splitlines())
    return posting_mode(switches.get("WORK_MAP_POSTING_PAUSED"), switches.get("WORK_MAP_REVIEW_ROTATIONS_ENABLED"))


def allows_post(mode: str, fresh: dict | None = None, *, channel: str | None = None) -> bool:
    return mode == "all" or (mode == "rotations" and fresh is not None
                             and fresh.get("kind") != "import-status" and fresh.get("sourceRotation") is True
                             and (channel or fresh.get("channel")) == "request")


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


def load_registry(path: Path) -> tuple[dict, str]:
    # Git names a blob by hashing its type, byte length and contents.
    data = path.read_bytes()
    header = f"blob {len(data)}\0".encode()
    oid = hashlib.sha1(header + data, usedforsecurity=False).hexdigest()
    return json.loads(data), oid


def registry_is_published(oid: str) -> bool:
    return api(f"repos/{FORK}/contents/registry.json?ref=gh-pages").get("sha") == oid


def confirmation(scheduled: dict, fresh: dict | None) -> dict:
    if not fresh or any(fresh.get(key) != scheduled[key] for key in
                        ("number", "head", "reviewer", "channel", "key", "body")):
        return {"status": "superseded"}
    versions = {"head": fresh["head"], "base": fresh.get("base")}
    if fresh.get("status") == "verify-mergeability" or fresh.get("mergeable") == "UNKNOWN":
        return {**versions, "status": "mergeability-unavailable"}
    if (fresh.get("status") != "due" or fresh.get("mergeable") != "MERGEABLE"
            or not re.fullmatch(r"[0-9a-f]{40}", versions["base"] or "")):
        return {"status": "superseded"}
    return {**versions, "status": "due"}


def save_native_denial(action: dict, status: int) -> None:
    """Record a rejected native request on the fork without replacing a newer receipt."""
    path = f"repos/{FORK}/contents/outreach-holds.json"
    env = {**os.environ, "GH_TOKEN": os.environ["WORK_MAP_RECEIPT_TOKEN"]}
    receipt = {key: action[key] for key in ("head", "reviewer", "lastContactAt")}
    receipt.update(http=status, observedAt=datetime.now(timezone.utc).isoformat())
    for _ in range(3):
        raw = subprocess.check_output(["gh", "api", "--hostname", "github.com", path + "?ref=gh-pages"],
                                      env=env, text=True, timeout=30)
        file = json.loads(raw)
        holds = json.loads(base64.b64decode(file["content"]))
        previous = holds.get(str(action["number"]))
        if previous and previous["observedAt"] >= receipt["observedAt"]:
            return
        holds[str(action["number"])] = receipt
        content = base64.b64encode((json.dumps(holds, indent=2, sort_keys=True) + "\n").encode()).decode()
        data = {"branch": "gh-pages", "sha": file["sha"], "content": content,
                "message": f"Record the rejected native review request on #{action['number']}"}
        result = subprocess.run(["gh", "api", "--hostname", "github.com", "--method", "PUT", path, "--input", "-"],
                                input=json.dumps(data), env=env, text=True, capture_output=True, timeout=30)
        if result.returncode == 0:
            return
        if "(HTTP 409)" not in result.stderr:
            raise RuntimeError(f"Could not publish the native request receipt: {result.stderr.strip()}")
    raise RuntimeError("Concurrent fork updates prevented the native request receipt from being recorded")


def send_one(action: dict, refresh: Callable[[], dict | None], owner: str, registry_oid: str) -> dict:
    number = action["number"]
    reviewer = action["reviewer"]
    marker = f"<!-- gvisor-review-outreach {action['key']} -->"
    if (not isinstance(number, int) or number < 1 or not re.fullmatch(r"[0-9a-f]{40}", action["head"])
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,38}", reviewer) or reviewer.lower() == owner.lower()
            or action["channel"] not in {"request", "comment"} or not action["body"].endswith(marker)
            or f"@{reviewer}" not in action["body"]):
        raise ValueError("Invalid review-outreach destination or body")
    current = refresh()
    checked = confirmation(action, current)
    if checked["status"] != "due":
        return checked
    if not registry_is_published(registry_oid):
        return {"status": "registry-changed"}

    requests = f"repos/{REPO}/pulls/{number}/requested_reviewers"
    if action["channel"] == "request":
        def pending() -> bool:
            observed = api(requests + "?per_page=100")
            return any(row["login"].lower() == reviewer.lower() for row in observed["users"])

        mode = live_posting_mode()
        if not allows_post(mode, current, channel="request"):
            return {"status": "held"}
        if mode == "rotations":
            # Another run may have recorded a denial since this checkout was published.
            published = api(f"repos/{FORK}/contents/outreach-holds.json?ref=gh-pages")
            hold = json.loads(base64.b64decode(published["content"])).get(str(number))
            if hold and hold["head"] == current["head"] and hold["lastContactAt"] == current["lastContactAt"]:
                return {**checked, "status": "native-request-denied", "reviewer": hold["reviewer"], "http": hold["http"]}
        try:
            api(requests, {"reviewers": [reviewer]})
        except GitHubError as error:
            if error.limited:
                raise
            # A timeout or 5xx can follow a successful write.
            if pending():
                return {**checked, "status": "request-observed", "reviewer": reviewer}
            if error.status not in (403, 404, 422):
                raise
            if mode == "rotations" or live_posting_mode() == "rotations":
                if not current.get("sourceRotation") or not current.get("lastContactAt"):
                    return {"status": "held"}
                save_native_denial(current, error.status)
                return {**checked, "status": "native-request-denied", "reviewer": reviewer, "http": error.status}
            # A definite rejection can mean the PR author lacks triage access.
            # Re-read the PR and the registry before falling back to a mention.
            current = refresh()
            checked = confirmation(action, current)
            if checked["status"] != "due":
                return checked
            if not registry_is_published(registry_oid):
                return {"status": "registry-changed"}
        else:
            if not pending():
                raise GitHubError(None, "Review request was acknowledged but is no longer pending")
            return {**checked, "status": "requested", "reviewer": reviewer}

    comments = f"repos/{REPO}/issues/{number}/comments"
    if not allows_post(live_posting_mode(), current, channel="comment"):
        return {"status": "held"}
    posted = api(comments, {"body": action["body"]})
    comment_id = posted.get("id")
    if type(comment_id) is not int:
        raise GitHubError(None, "Cannot identify the submitted review reminder")
    saved = api(f"repos/{REPO}/issues/comments/{comment_id}")
    if (saved.get("id") != comment_id or saved.get("user", {}).get("login") != owner
            or saved.get("body") != action["body"] or saved.get("issue_url") != f"https://api.github.com/repos/{REPO}/issues/{number}"):
        raise GitHubError(None, f"Could not verify the posted comment on #{number}")
    return {**checked, "status": "commented", "url": saved["html_url"], "fallback": action["channel"] == "request"}


def deliver(snapshot: dict, registry: dict, now: str | datetime, owner: str, maintainers: set[str],
            fetch_fresh: Callable[[int], dict], *, active_reviewers: set[str], post: bool, registry_oid: str, mode: str) -> None:
    from outreach import outreach_states, plan, reviewer_history

    if owner != "tamird" or registry["meta"]["repo"] != REPO:
        raise ValueError("Review outreach is configured only for tamird's gVisor PRs")
    plans = sorted((a for a in snapshot.get("reviewOutreach", {}).values()
                    if a.get("status") in {"due", "verify-mergeability"} and allows_post(mode, a)),
                   key=lambda a: (a["dueAt"], a["number"]))
    if mode != "all":
        held = [a["number"] for a in snapshot.get("reviewOutreach", {}).values()
                if a.get("status") in {"due", "verify-mergeability"} and not allows_post(mode, a)]
        print(json.dumps({"outreachPostingMode": mode, "held": sorted(held)}))
    if not plans:
        return
    if post and mode == "rotations" and not os.environ.get("WORK_MAP_RECEIPT_TOKEN"):
        print(json.dumps({"outreachPostingMode": mode, "held": [a["number"] for a in plans],
                          "reason": "No fork credential to record rejected native review requests"}))
        return
    if post and api("user").get("login") != owner:
        raise RuntimeError("Review-outreach credential does not belong to the configured owner")
    native_denials = json.loads(Path(__file__).with_name("outreach-holds.json").read_text())
    nodes = {node["number"]: node for node in registry["nodes"] if node.get("type") == "pr" and node.get("repo") == REPO}
    specialties = {name: set(areas) for name, areas in snapshot.get("reviewerSpecialties", {}).items()}
    history = reviewer_history(snapshot["prs"], owner, maintainers=active_reviewers, specialties=specialties,
                               observations=snapshot.get("reviewerActivity", {}).get("observations", []),
                               authored=snapshot.get("reviewerActivity", {}).get("authored", []))
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
            current = plan(pr, nodes.get(number, {}), outreach_states(latest, registry).get(str(number), {}),
                           at, owner, maintainers, history, preferred=scheduled["reviewer"],
                           rotations_only=mode == "rotations", native_denial=native_denials.get(str(number)))
            return {**current, "base": pr.get("base"), "mergeable": pr.get("mergeable")} if current else None

        if not post:
            current = refresh()
            result = confirmation(scheduled, current)
            if result["status"] == "due":
                result["status"] = "would-send" if allows_post(mode, current) else "held"
        else:
            try:
                result = send_one(scheduled, refresh, owner, registry_oid)
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
