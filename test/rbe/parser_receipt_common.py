"""Read original parser results by source; never rerun Bazel or export raw logs."""
import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

carrier = "e5621f9a6ce9e5ea1a706f2fdc1f53c82cd1c89d"
product = os.environ["PRODUCT"]
label = "//tools/nvidia_driver_differ:driver_ast_parser"
dest = Path(os.environ["RUNNER_TEMP"]) / "parser-receipt"
dest.mkdir(exist_ok=True)
report = {"originalCarrier": carrier, "product": product, "target": label,
          "invocations": [], "lookup": {}, "evidenceStatus": "NO MATCH"}

def save():
    (dest / "original.json").write_text(json.dumps(report, indent=2) + "\n")

def call(method, **args):
    url = "https://app.buildbuddy.io/" + (
        "rpc/BuildBuddyService/" if method in ("SearchInvocation", "GetExecution") else "api/v1/") + method
    request = urllib.request.Request(url, data=json.dumps(args).encode(), headers={
        "Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    with urllib.request.urlopen(request, timeout=50) as response:
        return json.load(response)

def pages(method, **args):
    seen = set()
    while len(seen) < 250:
        record = call(method, **args)
        yield record
        token = record.get("nextPageToken") or record.get("next_page_token")
        if not token:
            return
        if token in seen:
            raise ValueError("duplicate page")
        seen.add(token)
        args["page_token"] = token
    raise ValueError("page limit")

def get(row, *fields):
    return {k: row[k] for k in fields if k in row}

def execution(iid):
    # This service reports remote executions only. A local TdGenerate won't
    # appear here; the absence of a row isn't evidence of its success/failure.
    rows = call("GetExecution", executionLookup={"invocationId": iid}).get("execution", [])
    classes, failures, latest = Counter(), Counter(), []
    for r in rows:
        m = r.get("actionMnemonic", "?")
        classes[m] += 1
        status = r.get("status", {}).get("code", 0)
        ret = r.get("exitCode", 0)
        if status or ret:
            failures[f"{m} RPC {status} exit {ret}"] += 1
        meta = r.get("executedActionMetadata", {})
        finish = meta.get("workerCompletedTimestamp") or meta.get("executionCompletedTimestamp") or ""
        if finish and isinstance(finish, str):
            latest.append((finish, m, r.get("targetLabel", "")[:150], status, ret))
    return {"count": len(rows), "mnemonics": classes, "failures": failures,
            "lastRemoteCompletions": sorted(latest)[-8:]}

def logs(iid):
    counts = Counter()
    clues = []
    signatures = ("Resource Exhausted", "Killed signal terminated", "signal: killed", "No space left on device",
                  "Out of memory", "Maximum heap size", "The operation was canceled", "Received interrupt",
                  "Interrupted", "FAILED: Build did NOT complete", "ERROR: Build did NOT complete",
                  "Build completed successfully", "RISCVTargetParserDef.inc", "remote cache hit",
                  "connection reset", "Lost connection", "No such file or directory", "Timed out", "TIMEOUT")
    for r in pages("GetLog", selector={"invocation_id": iid}):
        for line in r.get("log", {}).get("contents", "").splitlines():
            for marker in signatures:
                if marker in line:
                    counts[marker] += 1
            # Allowlisted scalar details, no raw commands, headers or URLs.
            for pat in (r"Elapsed time: [0-9.]+s, Critical Path: [0-9.]+s",
                        r"[0-9]+ processes: [0-9a-z ,.]+", r"\(Exit [0-9]+\)",
                        r"bazel-out/[a-zA-Z0-9_.-]+/bin/tools/nvidia_driver_differ/driver_ast_parser"):
                m = re.search(pat, line)
                if m and m[0] not in clues and len(clues) < 30:
                    clues.append(m[0])
    return {"counts": counts, "details": clues}
