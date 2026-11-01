"""Recover only the original supplier invocation's remote execution failure."""
import base64
import datetime
import json
import os
import re
from pathlib import Path
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)
key = os.environ["BUILDBUDDY_API_KEY"]


def fetch(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request("https://app.buildbuddy.io/" + path, data=data,
        headers={"x-buildbuddy-api-key": key, "Content-Type": "application/json"})
    with opener.open(request, timeout=40) as response:
        return response.read()


def rpc(method, payload):
    return json.loads(fetch("rpc/BuildBuddyService/" + method, payload))


EXPECTED = {"//src:bazel", "//tools/test:posix_tw", "//tools/test:tw_test"}
STARTED = datetime.datetime.fromisoformat("2026-11-01T09:05:12.264276+00:00").timestamp()
SOURCES = {"b70c334832ca2a1109a8b5e8cdc632b411174b6f", "326721f4fd556e72000849a4053b1afaaec4f3d1"}
FINISHED = datetime.datetime.fromisoformat("2026-11-01T09:07:23.269961+00:00").timestamp()
result = {"status": "INCOMPLETE", "original_run": 41291507672, "new_executions": 0}
try:
    response = rpc("SearchInvocation", {"query": {"command": "test",
        "updatedAfter": "2026-11-01T08:05:00Z", "updatedBefore": "2026-11-01T10:10:00Z"}, "count": 100})
    if response.get("nextPageToken"):
        raise ValueError("The bounded invocation search is incomplete")
    # Search summaries use ShortFormatPatterns and omit the final label.
    candidates = [i for i in response.get("invocation", []) if (i.get("pattern") or [None])[0] == "//src:bazel"]
    result["search_count"] = len(response.get("invocation", []))
    result["first_pattern_count"] = len(candidates)
    result["matching_command_receipts"] = []
    selected = []
    for candidate in candidates:
        identifier = candidate["invocationId"]
        data = rpc("GetInvocation", {"lookup": {"invocationId": identifier}})
        exact = [i for i in data.get("invocation", []) if i.get("invocationId") == identifier]
        if len(exact) != 1:
            raise ValueError("Ambiguous invocation identity")
        invocation = exact[0]
        events = [event.get("buildEvent", {}) for event in invocation.get("event", [])]
        # GetInvocation also retains the short table summary; use the actual BEP.
        patterns = [event.get("id", {}).get("pattern", {}).get("pattern", []) for event in events]
        if not any(set(pattern) == EXPECTED for pattern in patterns):
            continue
        finishes = [event["finished"] for event in events if "finished" in event]
        if len(finishes) != 1:
            continue
        finish = finishes[0]
        milliseconds = finish.get("finishTimeMillis")
        seconds = int(milliseconds) / 1000 if milliseconds is not None else datetime.datetime.fromisoformat(finish["finishTime"]).timestamp()
        source = invocation.get("commitSha")
        result["matching_command_receipts"].append({
            "source_expected": source in SOURCES if source else None,
            "finish_seconds": seconds, "exit_code": finish.get("exitCode", {}).get("code", 0)})
        if source and source not in SOURCES:
            continue
        if STARTED <= seconds <= FINISHED and finish.get("exitCode", {}).get("code", 0) == 34:
            selected.append((identifier, invocation, seconds))
    if len(selected) != 1:
        raise ValueError("Expected one original command with its terminal time and exit34")
    identifier, invocation, finished = selected[0]
    result.update(invocation=identifier, original_finish_seconds=finished, original_exit=34,
                  targets=sorted(EXPECTED))
    chunks = rpc("GetEventLogChunk", {"invocationId": identifier, "minLines": 120})
    content = base64.b64decode(chunks.get("buffer", "")).decode(errors="replace")
    content += "\n" + invocation.get("consoleBuffer", "")
    executions = rpc("GetExecution", {"executionLookup": {"invocationId": identifier}})
    codes = {3: "INVALID_ARGUMENT", 4: "DEADLINE_EXCEEDED", 5: "NOT_FOUND", 7: "PERMISSION_DENIED",
             8: "RESOURCE_EXHAUSTED", 9: "FAILED_PRECONDITION", 13: "INTERNAL", 14: "UNAVAILABLE", 16: "UNAUTHENTICATED"}
    result["execution_status_codes"] = sorted({int(e.get("status", {}).get("code", 0)) for e in executions.get("execution", [])})
    for execution in executions.get("execution", []):
        status = execution.get("status", {})
        if int(status.get("code", 0)) in codes:
            content += "\n" + codes[int(status["code"])] + ": " + status.get("message", "")
    content = content.replace(key, "[REDACTED]")
    lines = []
    statuses = set()
    for line in content.splitlines():
        # Export the failure itself, never incidental image/pool/worker chatter.
        if not re.search(r"^(?:\s*Caused by: )?\s*(?:io\.grpc\.)?StatusRuntimeException:|^\s*(?:INVALID_ARGUMENT|UNAVAILABLE|FAILED_PRECONDITION|PERMISSION_DENIED|NOT_FOUND|RESOURCE_EXHAUSTED|INTERNAL|UNKNOWN|UNAUTHENTICATED|DEADLINE_EXCEEDED):", line):
            continue
        if re.search(r"api.key|remote.header|bes.header|authorization", line, re.I):
            continue
        statuses.update(re.findall(r"\b(?:INVALID_ARGUMENT|UNAVAILABLE|FAILED_PRECONDITION|PERMISSION_DENIED|NOT_FOUND|RESOURCE_EXHAUSTED|INTERNAL|UNKNOWN|UNAUTHENTICATED|DEADLINE_EXCEEDED)\b", line))
        line = re.sub(r"(?:https?|grpcs?|bytestream)://\S+", "[URL]", line)
        line = re.sub(r"\b[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)+(?:@sha256:[a-f0-9]+|:[A-Za-z0-9_.-]+)", "[IMAGE]", line)
        line = re.sub(r"\S+@\S+", "[USERINFO]", line)
        line = re.sub(r"\b(?:[A-Za-z0-9_-]+\.)+[A-Za-z0-9_-]+:\d+\b", "[ENDPOINT]", line)
        line = re.sub(r"\b(?:[A-Za-z0-9_-]+\.)+(?:com|io|net|org|internal|local|dev|cloud|app|build)\b", "[HOST]", line)
        line = re.sub(r"\bGR[A-Za-z0-9]+\b", "[GROUP]", line)
        line = re.sub(r"(?i)\b(worker|executor|host)(?:_id)?[=:]\s*[^,\s]+", r"\1=[REDACTED]", line)
        lines.append(line[:1000])
    result["diagnostic_lines"] = list(dict.fromkeys(lines))[-12:]
    result["grpc_statuses"] = sorted(statuses)
    if not lines or not statuses:
        raise ValueError("Original terminal gRPC status is not present in retained logs")
    result["status"] = "RECOVERED_LOGS"
except Exception as err:
    result["error"] = str(err).replace(key, "[REDACTED]")[:800]
finally:
    result["verified_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    (Path(os.environ["RUNNER_TEMP"]) / "supplier-diagnostic.json").write_text(json.dumps(result, indent=2) + "\n")
raise SystemExit(result["status"] != "RECOVERED_LOGS")
