"""Sample retries of gVisor's public GitHub Actions validation jobs."""

import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# These workflows each have a single, non-matrix validation job. For Go,
# generation and building the exported sources are separate validation steps.
WORKFLOWS = {
    "Lint": ("lint.yml", "lint", {"Run make lint"}),
    "Gazelle": ("gazelle.yml", "gazelle", {"Run if ! bazel run //:gazelle_check; then"}),
    "License header": ("fileheader.yml", "fileheader", {"Check license headers"}),
    "Go": ("go.yml", "generate", {"Run tools/go_branch.sh", "Run git checkout go && git clean -xf . && go build ./..."}),
}
INTERVAL = timedelta(hours=1)
SAMPLE = "Up to 100 recently created pull-request runs per workflow. One workflow is sampled each hour when the request budget permits."


def _read(path: str) -> dict:
    result = subprocess.run(["gh", "api", "repos/google/gvisor/actions/" + path],
                            text=True, capture_output=True, timeout=20, check=False)
    if result.returncode:
        status = re.search(r"HTTP (\d{3})\b", result.stderr)
        raise ValueError("GitHub Actions returned HTTP " + status[1] if status else "GitHub Actions request failed")
    return json.loads(result.stdout)


def _id(candidate: dict) -> str:
    return f'{candidate["run"]}/{candidate["attempt"]}/{candidate["head"]}'


def _job_pair(row: dict) -> tuple:
    return row["run"], row["head"], row["failedJob"], row["passedJob"]


def _discover(workflow: str) -> list[dict]:
    filename, _, _ = WORKFLOWS[workflow]
    data = _read(f"workflows/{filename}/runs?event=pull_request&per_page=100")
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


def _recoveries(candidate: dict) -> list[dict]:
    run, head, attempt = candidate["run"], candidate["head"], candidate["attempt"]
    _, name, steps = WORKFLOWS[candidate["workflow"]]
    data = _read(f"runs/{run}/jobs?filter=all&per_page=100")
    jobs = data["jobs"]
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("GitHub Actions returned no usable jobs for this run")
    if len(jobs) != data["total_count"]:
        return []  # A second job with the same name could be on another page.
    attempts = defaultdict(list)
    for job in jobs:
        if job["run_id"] != run or job["head_sha"] != head or job["name"] != name:
            return []
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
    return recovered


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
        try:
            if slot == "discoveryAt":
                cursor = state.get("cursor", 0)
                workflow = list(WORKFLOWS)[cursor]
                found = _discover(workflow)
                known = set(seen) | {_id(item) for item in pending}
                pending.extend(item for item in found if _id(item) not in known)
                pending = pending[:32]
                state["cursor"] = (cursor + 1) % len(WORKFLOWS)
            else:
                candidate = pending[0]
                for row in _recoveries(candidate):
                    observations[_job_pair(row)] = row
                seen.append(_id(candidate))
                pending.pop(0)
            state["checkedAt"] = stamp
            state["status"] = "ok"
            state.pop("error", None)
        except (subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError) as exc:
            if slot == "detailAt":
                # An unallocated run may stay empty. Retry it after the other candidates.
                pending.append(pending.pop(0))
            state["status"] = "unavailable"
            state["error"] = str(exc)[:160] if type(exc) is ValueError else "Unable to read GitHub Actions metadata"
            break
    state["pending"], state["seen"] = pending, seen[-128:]
    state["observations"] = sorted(observations.values(), key=lambda row: row["finishedAt"], reverse=True)[:64]
    return state
