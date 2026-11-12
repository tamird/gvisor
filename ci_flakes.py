"""Collect Buildkite job attempts which failed and subsequently passed on retry."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import http.client
import json
import re
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


PIPELINES = ("pipeline", "pgo-profiles", "release")
ROOT = "https://buildkite.com/gvisor"
# A delayed build gets a fresh turn independently of newer build numbers.
NEW_PER_SCAN = 4
PENDING_PER_SCAN = 2
LOOKBACK = 32
MAX_PENDING = 128
MAX_PAGES = 4
MAX_OBSERVATIONS = 800
RETENTION = timedelta(days=28)
RETRY_WINDOW = timedelta(days=1)
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
HEAD = re.compile(r"[0-9a-f]{40}")
PR = re.compile(r"https://github\.com/google/gvisor/pull/([1-9][0-9]*)")
ERRORS = (OSError, ValueError, http.client.HTTPException)


def read(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "gvisor-work-map"})
    with urlopen(request, timeout=5) as response:
        if urlsplit(response.geturl()).netloc != "buildkite.com":
            raise ValueError("Buildkite redirected to another host")
        data = response.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("Buildkite response exceeded the size limit")
    return data


def latest_build(slug: str) -> int:
    listing = read(f"{ROOT}/{slug}/builds").decode()
    numbers = re.findall(rf"/gvisor/{slug}/builds/([1-9][0-9]*)(?![0-9])", listing)
    if not numbers:
        raise ValueError("No build numbers in the public pipeline listing")
    return max(map(int, numbers))


def job_records(url: str) -> dict[str, dict]:
    jobs = {}
    cursors = set()
    page_url = url
    for _ in range(MAX_PAGES):
        page = json.loads(read(page_url))
        if not isinstance(page, dict) or not isinstance(page.get("records"), list):
            raise ValueError("Invalid Buildkite jobs page")
        for job in page["records"]:
            if (not isinstance(job, dict) or not isinstance(key := job.get("id"), str) or key in jobs
                    or not isinstance(job.get("state"), str)
                    or job.get("retried_in_job_uuid") is not None and not isinstance(job["retried_in_job_uuid"], str)):
                raise ValueError("Invalid or repeated job in Buildkite jobs page")
            jobs[key] = job
        if page.get("has_next_page") is False:
            return jobs
        cursor = page.get("end_cursor")
        if page.get("has_next_page") is not True or not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ValueError("Incomplete Buildkite job pagination")
        cursors.add(cursor)
        page_url = f"{url}?after={quote(cursor, safe='')}"
    raise ValueError("Buildkite job pagination exceeded the scan limit")


def failed(job: dict) -> bool:
    return (job.get("state") in {"finished", "timed_out", "expired"}
            and job.get("passed") is False and not job.get("soft_failed"))


def recoveries(slug: str, number: int, build: dict, jobs: dict[str, dict]) -> list[dict]:
    # Keep the first failed attempt, even if it took several further attempts
    # to pass. The provider's UUID links distinguish identically named shards.
    retrying = {key for key, job in jobs.items() if failed(job) and job.get("retried_in_job_uuid")}
    retried_failures = {jobs[key]["retried_in_job_uuid"] for key in retrying}
    records = []
    traversed = set()
    for start_id in retrying - retried_failures:
        start = jobs[start_id]
        if not UUID.fullmatch(start_id) or not isinstance(name := start.get("name"), str) or not name:
            raise ValueError("Invalid failed Buildkite job identity")
        seen = {start_id}
        last = start
        while next_id := last.get("retried_in_job_uuid"):
            if not isinstance(next_id, str) or not UUID.fullmatch(next_id) or next_id in seen or next_id not in jobs:
                raise ValueError("Incomplete or cyclic Buildkite retry chain")
            seen.add(next_id)
            last = jobs[next_id]
            if any(last.get(field) != start.get(field) for field in (
                    "name", "step_key", "group_uuid", "parallel_group_index", "parallel_group_total")):
                raise ValueError("Buildkite retry changed job identity")
        traversed.update(seen)
        if last.get("retry_pending") or last["state"] not in {"finished", "timed_out", "expired", "broken", "skipped", "canceled"}:
            raise ValueError("Retry still active after build reported completion")
        if last["state"] != "finished" or last.get("passed") is not True:
            continue
        finished = last.get("finished_at")
        if not isinstance(finished, str) or datetime.fromisoformat(finished.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("Missing passing job completion time")
        record = {"pipeline": slug, "build": number, "head": build["commit_id"], "job": name,
                  "failedJob": start_id, "failedState": start["state"], "passedJob": last["id"], "finishedAt": finished,
                  "exitCode": start.get("exit_status") if type(start.get("exit_status")) is int else None}
        if isinstance(pr := build.get("pull_request"), dict) and (match := PR.fullmatch(str(pr.get("url")))):
            record["pr"] = int(match[1])
        records.append(record)
    if retrying - traversed:
        raise ValueError("Cyclic Buildkite retry chain")
    return records


def inspect(slug: str, number: int, now: datetime) -> tuple[list[dict], bool]:
    base = f"{ROOT}/{slug}/builds/{number}"
    build = json.loads(read(base + ".json"))
    if not isinstance(build, dict) or build.get("number") != number or build.get("path") != f"/gvisor/{slug}/builds/{number}":
        raise ValueError("Buildkite build identity mismatch")
    if not isinstance(status := build.get("state"), str):
        raise ValueError("Buildkite did not report a build state")
    if status not in {"passed", "failed", "canceled", "skipped", "not_run"}:
        return [], True
    if not isinstance(head := build.get("commit_id"), str) or not HEAD.fullmatch(head):
        raise ValueError("Buildkite did not report an exact commit")
    # A maintainer can retry a failed job after the build finishes.
    revisit = False
    if status == "failed" and isinstance(finished := build.get("finished_at"), str):
        at = datetime.fromisoformat(finished.replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError("Missing build completion time zone")
        revisit = now - at < RETRY_WINDOW
    jobs = job_records(base + "/data/jobs")
    return recoveries(slug, number, build, jobs), revisit or any(job.get("retry_pending") for job in jobs.values())


def scan(slug: str, previous: dict, now: str) -> tuple[dict, list]:
    started = time.monotonic()
    at = datetime.fromisoformat(now.replace("Z", "+00:00"))
    state = dict(previous, checkedAt=now, status="ok")
    state.pop("error", None)
    try:
        latest = latest_build(slug)
    except ERRORS as error:
        return {**state, "status": "unavailable", "error": str(error)}, []
    state["latest"] = max(latest, state.get("latest", latest))
    state.setdefault("firstBuild", max(1, latest - LOOKBACK + 1))
    next_build = state.get("nextBuild", state["firstBuild"])
    pending = list(state.get("pending", []))
    stop = min(state["latest"] + 1, next_build + NEW_PER_SCAN)
    fresh = list(range(next_build, max(next_build, stop)))
    state["nextBuild"] = next_build + len(fresh)
    retry, pending = pending[:PENDING_PER_SCAN], pending[PENDING_PER_SCAN:]
    records = []
    work = [*retry, *fresh]
    for i, number in enumerate(work):
        if time.monotonic() - started > 25:
            state.update(status="partial", error="Buildkite refresh time budget reached")
            pending.extend(work[i:])
            break
        try:
            found, revisit = inspect(slug, number, at)
        except ERRORS as error:
            state.update(status="partial", error=f"Build #{number}: {error}")
            if isinstance(error, HTTPError) and error.code in (404, 410):
                # Sequential numbers include builds deleted by the provider.
                state["missingBuilds"] = (state.get("missingBuilds", []) + [number])[-LOOKBACK:]
                continue
            found, revisit = [], True
        if revisit:
            pending.append(number)
        records.extend(found)
    if len(pending) > MAX_PENDING:
        state.update(status="partial", error="Unfinished builds exceeded the retry queue limit")
        missed = pending[:-MAX_PENDING]
        state["unobservedBuilds"] = (state.get("unobservedBuilds", []) + missed)[-LOOKBACK:]
        pending = pending[-MAX_PENDING:]
    state["pending"] = pending
    return state, records


def collect(previous: dict, now: str) -> dict:
    """Refresh each public pipeline independently of GitHub PR ownership."""
    cutoff = datetime.fromisoformat(now.replace("Z", "+00:00")) - RETENTION
    states = previous.get("pipelines", {})
    observations = {(r["pipeline"], r["failedJob"]): r for r in previous.get("observations", [])
                    if datetime.fromisoformat(r["finishedAt"].replace("Z", "+00:00")) >= cutoff}
    pipelines = {}
    with ThreadPoolExecutor(max_workers=len(PIPELINES)) as pool:
        work = {p: pool.submit(scan, p, states.get(p, {}), now) for p in PIPELINES}
        for slug, future in work.items():
            pipelines[slug], records = future.result()
            for r in records:
                if datetime.fromisoformat(r["finishedAt"].replace("Z", "+00:00")) >= cutoff:
                    observations[(slug, r["failedJob"])] = r
    latest = sorted(observations.values(), key=lambda r: (r["finishedAt"], r["pipeline"], r["failedJob"]), reverse=True)
    return {"checkedAt": now, "pipelines": pipelines, "observations": latest[:MAX_OBSERVATIONS]}
