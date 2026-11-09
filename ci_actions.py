"""Sample retries of gVisor's public GitHub Actions validation jobs."""

import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

# These workflows each have a single, non-matrix validation job. For Go,
# generation and building the exported sources are separate validation steps.
WORKFLOWS = {
    "Lint": ("lint.yml", "lint", {"Run make lint"}),
    "Gazelle": ("gazelle.yml", "gazelle", {"Run if ! bazel run //:gazelle_check; then"}),
    "License header": ("fileheader.yml", "fileheader", {"Check license headers"}),
    "Go": ("go.yml", "generate", {"Run tools/go_branch.sh", "Run git checkout go && git clean -xf . && go build ./..."}),
}
INTERVAL = timedelta(hours=1)
ACTIONS = "repos/google/gvisor/actions"
DISCOVERY = ("reports", *WORKFLOWS)
SAMPLE = ("Up to 100 recently created PR runs per workflow and 100 jobs per run. "
          "Larger job histories are excluded. One discovery each hour rotates between public reports "
          "and four workflows when the request budget permits.")
REPORT_SAMPLE = ("Up to 25 gVisor issues and PRs updated in the last 90 days, newest update first. "
                 "GitHub searches their titles for flake, flaky or intermittent.")


def _read(path: str) -> dict:
    result = subprocess.run(["gh", "api", path],
                            text=True, capture_output=True, timeout=20, check=False)
    if result.returncode:
        status = re.search(r"HTTP (\d{3})\b", result.stderr)
        raise ValueError("GitHub Actions returned HTTP " + status[1] if status else "GitHub Actions request failed")
    return json.loads(result.stdout)


def _id(candidate: dict) -> str:
    return f'{candidate["run"]}/{candidate["attempt"]}/{candidate["head"]}'


def _job_pair(row: dict) -> tuple:
    return row["run"], row["head"], row["failedJob"], row["passedJob"]


def _reports(now: datetime) -> dict:
    start = (now - timedelta(days=90)).isoformat(timespec="seconds")
    end = now.isoformat(timespec="seconds")
    query = f"repo:google/gvisor in:title (flake OR flaky OR intermittent) updated:{start}..{end} sort:updated-desc"
    page = _read("search/issues?" + urlencode({"q": query, "per_page": 25}))
    rows, total = page["items"], page["total_count"]
    if page["incomplete_results"] or not isinstance(rows, list) or len(rows) != min(total, 25):
        raise ValueError("GitHub returned an incomplete flake report search")
    items = []
    for row in rows:
        number = row["number"]
        pull = row.get("pull_request")
        path = "pull" if pull else "issues"
        url = f"https://github.com/google/gvisor/{path}/{number}"
        if row["html_url"] != url or row["repository_url"] != "https://api.github.com/repos/google/gvisor":
            raise ValueError("Flake report does not identify a gVisor issue or PR")
        items.append({"number": number, "kind": "pr" if pull else "issue", "title": row["title"],
                      "url": url, "state": "merged" if pull and pull.get("merged_at") else row["state"],
                      "updatedAt": row["updated_at"]})
    return {"status": "ok", "checkedAt": now.isoformat(), "sample": REPORT_SAMPLE, "windowStart": start,
            "windowEnd": end, "total": total, "hasMore": total > len(items), "items": items}


def _discover(workflow: str) -> list[dict]:
    filename, _, _ = WORKFLOWS[workflow]
    data = _read(f"{ACTIONS}/workflows/{filename}/runs?event=pull_request&per_page=100")
    rows = data["workflow_runs"]
    if not isinstance(rows, list):
        raise ValueError("Invalid workflow run list")
    candidates = []
    for run in rows:
        if not (run["event"] == "pull_request" and run["path"] == ".github/workflows/" + filename
                and run["status"] == "completed" and run["conclusion"] == "success"
                and type(run["id"]) is int and type(run["run_attempt"]) is int
                and run["id"] > 0 and run["run_attempt"] >= 2 and re.fullmatch(r"[0-9a-f]{40}", run["head_sha"])):
            continue
        item = {"run": run["id"], "attempt": run["run_attempt"], "head": run["head_sha"], "workflow": workflow}
        # A fork run can legitimately have no pull_requests entry. Only attach
        # a PR that GitHub ties to this repository and the tested head.
        prs = run.get("pull_requests") or []
        if len(prs) == 1 and prs[0]["head"]["sha"] == item["head"] and prs[0]["base"]["repo"]["id"] == run["repository"]["id"]:
            item["pr"] = prs[0]["number"]
        candidates.append(item)
    return candidates


def _recoveries(candidate: dict) -> tuple[list[dict], dict | None]:
    run, head, attempt = candidate["run"], candidate["head"], candidate["attempt"]
    _, name, steps = WORKFLOWS[candidate["workflow"]]
    data = _read(f"{ACTIONS}/runs/{run}/jobs?filter=all&per_page=100")
    jobs = data["jobs"]
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("GitHub Actions returned no usable jobs for this run")
    if len(jobs) < data["total_count"]:
        # A second job with the same name could be on another page.
        return [], {**candidate, "observed": len(jobs), "total": data["total_count"]}
    if len(jobs) != data["total_count"]:
        raise ValueError("GitHub returned an inconsistent job count")
    attempts = defaultdict(list)
    for job in jobs:
        if job["run_id"] != run or job["head_sha"] != head or job["name"] != name:
            return [], None
        attempts[job["run_attempt"]].append(job)
    recovered = []
    for number in sorted(attempts):
        if number > attempt or len(attempts[number]) != 1 or len(attempts.get(number - 1, [])) != 1:
            continue
        failed, passed = attempts[number - 1][0], attempts[number][0]
        if (type(failed["id"]) is not int or type(passed["id"]) is not int or failed["id"] == passed["id"]
                or failed["status"] != "completed" or passed["status"] != "completed"
                or failed["conclusion"] != "failure" or passed["conclusion"] != "success"):
            continue
        # A setup failure or a skipped validation step does not demonstrate
        # that the same validation failed and then passed.
        failed_steps = [s["name"] for s in failed["steps"] if s["name"] in steps and s["conclusion"] == "failure"]
        passed_steps = [s["name"] for s in passed["steps"] if s["name"] in steps and s["conclusion"] == "success"]
        if len(failed_steps) != 1 or passed_steps.count(failed_steps[0]) != 1:
            continue
        if not (failed.get("completed_at") and passed.get("started_at") and passed.get("completed_at")
                and _time(failed["completed_at"]) <= _time(passed["started_at"]) <= _time(passed["completed_at"])):
            continue
        row = {"run": run, "workflow": candidate["workflow"], "job": name, "head": head,
               "failedAttempt": number - 1, "passedAttempt": number, "failedJob": failed["id"],
               "passedJob": passed["id"], "finishedAt": passed["completed_at"]}
        if "pr" in candidate:
            row["pr"] = candidate["pr"]
        recovered.append(row)
    return recovered, None


def _time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def collect(previous: dict | None, now: str | datetime, *, budget: int) -> dict:
    """Spend up to two spare GETs, preserving verified rows and unfinished work."""
    now = _time(now).astimezone(timezone.utc)
    stamp = now.isoformat()
    state = {"checkedAt": None, "status": "deferred", "observations": [], **(previous or {}),
             "sample": SAMPLE, "requestsUsed": 0}
    pending = list(state.get("pending", []))
    seen = list(state.get("seen", []))
    observations = {_job_pair(row): row for row in state["observations"]}
    for _ in range(min(budget, 2)):
        # Discovery and detail each get one slot per hour. With only one
        # spare request, serve the older slot first and keep the other due.
        slots = ["discoveryAt"] + (["detailAt"] if pending else [])
        due = [(state.get(slot, ""), slot) for slot in slots
               if not state.get(slot) or now - _time(state[slot]) >= INTERVAL]
        if not due:
            break
        _, slot = min(due)
        state[slot] = stamp
        state["requestsUsed"] += 1
        source = None
        if slot == "discoveryAt":
            cursor = state.get("cursor", 0)
            source = DISCOVERY[cursor]
            # A missing or rate-limited endpoint must not starve every other source.
            state["cursor"] = (cursor + 1) % len(DISCOVERY)
        try:
            status = "ok"
            if source == "reports":
                state["reports"] = _reports(now)
                continue
            if source:
                found = _discover(source)
                known = set(seen) | {_id(item) for item in pending}
                pending.extend(item for item in found if _id(item) not in known)
                pending = pending[:32]
            else:
                candidate = pending[0]
                recovered, partial = _recoveries(candidate)
                if partial:
                    state["partialRun"] = {**partial, "checkedAt": stamp}
                    status = "partial"
                for row in recovered:
                    observations[_job_pair(row)] = row
                seen.append(_id(candidate))
                pending.pop(0)
            state["checkedAt"] = stamp
            state["status"] = status
            state.pop("error", None)
        except (subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError) as exc:
            error = str(exc)[:160] if type(exc) is ValueError else "Unable to read GitHub CI metadata"
            if source == "reports":
                state["reports"] = {"items": [], "sample": REPORT_SAMPLE, **state.get("reports", {}),
                                    "status": "unavailable", "error": error, "attemptedAt": stamp}
                continue
            if slot == "detailAt":
                # An unallocated run may stay empty. Retry it after the other candidates.
                pending.append(pending.pop(0))
            state["status"] = "unavailable"
            state["error"] = error
            break
    state["pending"], state["seen"] = pending, seen[-128:]
    state["observations"] = sorted(observations.values(), key=lambda row: row["finishedAt"], reverse=True)[:64]
    return state
