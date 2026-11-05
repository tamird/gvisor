"""Read the two interrupted builds. No product code or actions are run."""
from collections import Counter
from datetime import datetime
import hashlib
import json
import re
import zipfile

from cas_inputs import TMP, fetch, rpc


SOURCE = "ddd06cb4a7d004178513c25c018ede20d8cc9449"
ORIGINAL = "eb1ddda0cde984951e50645f98e1f0b1bd0036304a092d0cc2aff105bfa80a6e"
INVOCATIONS = {
    "gnu": "2771ffe9-d5e5-4f69-bbc2-b9409a372785",
    "musl": "1cf6df38-1ed1-4400-a043-a7a13c951ce0",
}
TERM = r"[A-Za-z0-9_.+@:/~=-]{1,220}"
CATEGORIES = {
    "interrupted": r"interrupt|cancel",
    "remote_executor": r"remote execut|remote worker",
    "analysis": r"Analyzing:|Analyzed target",
    "fetch": r"[Ff]etching|[Dd]ownloading|Waiting for fetch",
    "repo_error": r"[Ee]rror.*repository|[Ee]rror.*download|failed to fetch|no such package",
    "capacity": r"ResourceExhausted|no registered executors|No free executors|No workers|no available executor|no matching executor|waiting for remote",
    "network": r"UNAVAILABLE|Connection refused|timed out|Read timed out|Connection reset",
    "disk_full": r"No space left on device|ENOSPC",
    "memory": r"OutOfMemoryError|Cannot allocate memory|out of memory",
    "credentials": r"PERMISSION_DENIED|UNAUTHENTICATED",
    "rate_limited": r"rate.?limit|HTTP[^\n]{1,20}429|Too Many Requests",
    "install": r"Extracting Bazel installation|Starting local Bazel server",
}


def logs(iid):
    req = {"selector": {"invocation_id": iid}}
    pages = []
    size = 0
    for _ in range(16):
        page = json.loads(fetch("api/v1/GetLog", req))
        chunk = page.get("log", {}).get("contents", "")
        pages.append(chunk)
        size += len(chunk)
        assert size <= 8 * 1024 * 1024
        token = page.get("nextPageToken", page.get("next_page_token"))
        if not token:
            break
        req["page_token"] = token
    else:
        raise ValueError("original log exceeds page bound")
    log = "".join(pages)
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", log)
    # Publish only public Bazel labels, repo names, phase counts and fixed
    # diagnostic categories. Command lines, URLs and arbitrary log lines stay private.
    groups = {
        "analyzing": r"Analyzing: target (" + TERM + ")",
        "configs": r"([\d,]+ packages loaded, [\d,]+ targets configured)",
        "action_counts": r"\[([\d,]+ / [\d,]+)\]",
        "repo_activity": r"(?:Fetching repository|fetch of repository|[Rr]epository) ['\"]?(@[+~\w.\-]+)",
        "canonical_repos": r"@@([A-Za-z0-9+_.~-]+)(?=//|[; \n])",
        "active_repos": r"(?:^|[\s(])(@[A-Za-z0-9+_.~-]+)(?:;|: (?:fetch|download))",
        "github_sources": r"https?://(?:codeload\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/",
        "remote_fetch": r"(?:https?://)([A-Za-z0-9.-]+)(?=/)",
        "fetch_target": r"(?:Downloading|Fetching|fetching)\s*(@[\w+.~/-]+)",
        "finished": r"(\d+ (?:processes|remote cache hit|remote|internal|local))\b",
        "action_seconds": r"\b([0-9]+s) [\w,-]*(?:remote|worker|disk-cache)",
    }
    numbers = {k: list(dict.fromkeys(re.findall(v, text)))[-18:] for k, v in groups.items()}
    # Bazel progress text before "; 123s remote" identifies a stuck action
    # even when the supplier did not retain its execution timestamps.
    progress = {}
    for event in re.finditer(r"\[[0-9,]+ / [0-9,]+\]\s*([^;\r\n]{1,240});\s*([0-9]+)s", text):
        action, elapsed = event.groups()
        if int(elapsed) < 60 or not re.fullmatch(r"[A-Za-z0-9 _+./(),:@\[\]~-]{1,200}", action):
            continue
        if any(secret in action.lower() for secret in ("//http", "://", "password", "bearer", "credential", "header", "token")):
            continue
        progress[action] = max(int(elapsed), progress.get(action, 0))
    numbers["long_remote_actions"] = sorted(progress.items(), key=lambda pair: pair[1], reverse=True)[:12]
    return {"bytes": len(log.encode()), "sha256": hashlib.sha256(log.encode()).hexdigest(),
            "indicators": {k: len(re.findall(v, text, re.IGNORECASE)) for k, v in CATEGORIES.items()},
            "progress": numbers}


def execution(item):
    iid = item["invocation"]
    rows = rpc("GetExecution", {"executionLookup": {"invocationId": iid}}).get("execution", [])
    assert len(rows) < 20000
    end = datetime.fromisoformat(item["command_finished"]).timestamp()
    stage_counts = Counter()
    status_counts = Counter()
    failures = []
    samples = []
    for row in rows:
        stage = row.get("stage", "UNKNOWN")
        mnemonic = row.get("actionMnemonic", "?")
        assert re.fullmatch(r"[a-zA-Z_0-9?]{1,80}", mnemonic)
        stage_counts[f"{stage}/{mnemonic}"] += 1
        status = row.get("status", {})
        code = status.get("code", 0)
        status_counts[str(code)] += 1
        row_label = row.get("targetLabel", "")
        if not re.fullmatch(TERM, row_label):
            row_label = "(no public label)"
        if code:
            message = status.get("message", "").lower()
            categories = [category for category in ("lease", "deadline", "cancel", "lost", "scheduler", "connection",
                          "shutdown", "full", "memory", "evicted", "worker", "executor", "unavailable")
                          if category in message]
            digest = row.get("actionDigest", {}).get("hash", "")
            if digest:
                assert re.fullmatch(r"[a-f0-9]{64}", digest)
            failures.append({"code": code, "stage": stage, "mnemonic": mnemonic, "label": row_label,
                             "digest": digest, "categories": categories})
        md = row.get("executedActionMetadata", {})
        queued = md.get("queuedTimestamp")
        if not queued:
            continue
        start = datetime.fromisoformat(queued.replace("Z", "+00:00")).timestamp()
        fin = md.get("executionCompletedTimestamp")
        seconds = round((datetime.fromisoformat(fin.replace("Z", "+00:00")).timestamp() if fin else end) - start)
        samples.append((seconds, stage, mnemonic, row_label, bool(md.get("workerStartTimestamp")),
                        bool(md.get("executionStartTimestamp")), bool(fin)))
    return {"rows": len(rows), "stages": dict(stage_counts), "grpc_codes": dict(status_counts),
            "non_ok": failures[:30], "longest": sorted(samples, key=lambda row: row[0], reverse=True)[:12]}


def main():
    original = TMP / "original.zip"
    assert hashlib.sha256(original.read_bytes()).hexdigest() == ORIGINAL
    with zipfile.ZipFile(original) as archive:
        control = json.loads(archive.read("result.json"))
    assert control["source"] == SOURCE and control["status"] == "FAIL"
    result = {"source": SOURCE, "artifact": 13446866250, "profiles": {}, "status": "INCOMPLETE"}
    for name, iid in INVOCATIONS.items():
        item = control["profiles"][name]
        assert item["invocation"] == iid and item["exit"] == 124 and item["attempts"] == []
        profile = {"invocation": iid}
        result["profiles"][name] = profile
        for key, read in [("log", lambda: logs(iid)), ("actions", lambda: execution(item))]:
            try:
                profile[key] = read()
            except Exception as error:
                profile[key + "_error"] = {"type": type(error).__name__, "http": getattr(error, "code", None),
                                           "line": error.__traceback__.tb_next.tb_lineno}
    result["status"] = "READ" if all("log" in p and "actions" in p for p in result["profiles"].values()) else "INCOMPLETE"
    (TMP / "musl-current-evidence/result.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
