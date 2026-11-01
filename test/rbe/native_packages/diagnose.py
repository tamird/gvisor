"""Recover only the two failed Go bootstrap actions; do not run a build."""
import base64
import json
import os
import re
from datetime import datetime
import urllib.parse
import urllib.request
from pathlib import Path

DIGESTS = {
    "amd64": "3cbbcd72629e840be3a527c7b6a0719a17122635d6dd141f0059b67d46011428",
    "arm64": "5ef694a9742e6cab1e3f56c5fb63e4e1523f1efbcd9e94a3fd4356343c35d958",
}

FINISH_MS = {"amd64": 1793521139114, "arm64": 1793521232481}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)
key = os.environ["BUILDBUDDY_API_KEY"]


def fetch(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        "https://app.buildbuddy.io/" + path, data=data,
        headers={"x-buildbuddy-api-key": key, "Content-Type": "application/json"},
    )
    with opener.open(request, timeout=45) as response:
        return response.read()


def rpc(method, payload):
    return json.loads(fetch("rpc/BuildBuddyService/" + method, payload))


out = Path(os.environ["RUNNER_TEMP"]) / "bootstrap-diagnostic.json"
result = {"status": "INCOMPLETE", "original_run": 41288127365, "executed_tests": 0, "actions": {}}
try:
    for arch, digest in DIGESTS.items():
        target = f"@@rules_go++go_sdk+main___download_0_linux_{arch}//:builder"
        search = rpc("SearchExecution", {"query": {
            "updatedAfter": "2026-11-01T07:50:00Z", "updatedBefore": "2026-11-01T09:30:00Z",
            "dimensionFilter": [{"dimension": {"execution": "TARGET_LABEL_EXECUTION_DIMENSION"}, "value": target}],
        }, "count": 50})
        selected = [row for row in search.get("execution", [])
                    if row["execution"].get("actionDigest", {}).get("hash") == digest]
        if not selected:
            result["actions"][arch] = {"status": "MISSING", "action_hash": digest,
                                      "matching_target_count": len(search.get("execution", [])),
                                      "more_results": bool(search.get("nextPageToken"))}
            continue
        candidates = {row["invocationMetadata"]["id"] for row in selected}
        if len(candidates) > 8:
            raise ValueError("too many candidate invocations")
        matches = []
        for invocation in candidates:
            original = rpc("GetInvocation", {"lookup": {"invocationId": invocation}})
            for item in original.get("invocation", []):
                for event in item.get("event", []):
                    finished = event.get("buildEvent", {}).get("finished", {})
                    milliseconds = finished.get("finishTimeMillis")
                    if milliseconds is None and finished.get("finishTime"):
                        milliseconds = round(datetime.fromisoformat(finished["finishTime"]).timestamp() * 1000)
                    if milliseconds is not None and int(milliseconds) == FINISH_MS[arch]:
                        matches.append(invocation)
        if len(matches) != 1:
            raise ValueError(f"{arch}: original BEP finish did not uniquely identify invocation")
        invocation = matches[0]
        records = rpc("GetExecution", {"executionLookup": {"invocationId": invocation,
                      "actionDigestHash": digest}, "inlineExecuteResponse": True})
        actions = [item for item in records["execution"] if item["actionDigest"]["hash"] == digest]
        if len(actions) != 1:
            raise ValueError(f"{arch}: expected one original action")
        action = actions[0]
        response = action.get("executeResponse", {})
        action_result = response.get("result", {})
        stderr = base64.b64decode(action_result.get("stderrRaw", ""))
        stderr_digest = action_result.get("stderrDigest")
        if not stderr and stderr_digest:
            url = "bytestream://remote.buildbuddy.io/blobs/" + stderr_digest["hash"] + "/" + str(stderr_digest["sizeBytes"])
            stderr = fetch("file/download?" + urllib.parse.urlencode({"bytestream_url": url, "invocation_id": invocation}))
        if action_result.get("exitCode") != 1 or action.get("stage") not in (4, "COMPLETED"):
            raise ValueError(f"{arch}: original terminal exit1 missing")
        text = stderr.decode(errors="replace").replace(key, "[REDACTED]")
        diagnostics = [line[:1000] for line in text.splitlines()
                       if re.search(r"error:|no such file|not found|permission denied|exec format|cannot execute|bad substitution|syntax error|go: |fork/exec", line, re.I)
                       and not re.search(r"api.key|remote.header|bes.header|authorization", line, re.I)][:12]
        if not diagnostics:
            raise ValueError(f"{arch}: no compiler/exec diagnostic in original stderr")
        result["actions"][arch] = {"status": "RECOVERED", "action_hash": digest,
            "invocation": invocation, "execution_id": action.get("executionId"),
            "exit_code": action_result.get("exitCode"), "execution_status": action.get("status"),
            "bep_finish_ms": FINISH_MS[arch], "stderr_digest": stderr_digest,
            "diagnostic_lines": diagnostics}
    if len(result["actions"]) == 2 and all(row["status"] == "RECOVERED" for row in result["actions"].values()):
        result["status"] = "PASS"
except Exception as err:
    result["error"] = str(err).replace(key, "[REDACTED]")[:1000]
finally:
    out.write_text(json.dumps(result, indent=2) + "\n")
raise SystemExit(0 if result["status"] == "PASS" else 1)
