"""Recover the original wrapper failures and zero-case Go output; run no tests."""
import base64
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
import urllib.parse
import urllib.request

ORIGINALS = {
    "amd64": {
        "invocation": "f95159a7-0430-48a0-b3f6-f20c9da875b9", "finish_ms": 1793522596969,
        "exit": 1, "actions": {
            "//pkg/state:state_test": "e4688d91bde4a989e8512619963cebb24edd892436a4c97328be130dfcbf80fe",
            "//pkg/tcpip/transport/tcp:tcp_test": "92a4ce9534a3c72901777d70a2727b263961e57794883ec49d68ced9e094ba54",
        },
    },
    "arm64": {
        "invocation": "27323794-3077-4611-a52c-6b7948b7f7af", "finish_ms": 1793522875477,
        "exit": 0, "actions": {
            "//pkg/state:state_test": "f19164ca30b329704b598d8428b62fde404d9f9de006f43008326b3b4035feef",
            "//pkg/tcpip/transport/tcp:tcp_test": "826519dafa6d026e09212b9cc2adb2f3ea7030cfcaa1eeee5efc319976724466",
        },
    },
}


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


def blob(digest, invocation):
    url = "bytestream://remote.buildbuddy.io/blobs/" + digest["hash"] + "/" + str(digest["sizeBytes"])
    data = fetch("file/download?" + urllib.parse.urlencode({"bytestream_url": url, "invocation_id": invocation}))
    if len(data) != int(digest["sizeBytes"]) or hashlib.sha256(data).hexdigest() != digest["hash"]:
        raise ValueError("Original output digest mismatch")
    return data


def project(data):
    lines = data.decode(errors="replace").replace(key, "[REDACTED]").splitlines()
    selected = [line[:1200] for line in lines
        if re.search(r"error|warning|no such file|not found|permission denied|exec format|cannot execute|bad substitution|syntax error|go: |fork/exec|PASS|FAIL|=== RUN|--- SKIP|no tests|test.*shard|test.*filter", line, re.I)
        and not re.search(r"api.key|remote.header|bes.header|authorization", line, re.I)]
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "diagnostic_lines": selected[:25]}


result = {"status": "INCOMPLETE", "original_run": 41289663114, "new_test_executions": 0, "architectures": {}}
try:
    for arch, original in ORIGINALS.items():
        invocation = original["invocation"]
        row = {"status": "INCOMPLETE", "invocation": invocation, "actions": {}, "test_logs": {}}
        result["architectures"][arch] = row
        response = rpc("GetInvocation", {"lookup": {"invocationId": invocation}})
        invocations = [item for item in response.get("invocation", []) if item.get("invocationId") == invocation]
        if len(invocations) != 1:
            raise ValueError(arch + ": expected exact original invocation")
        events = [item.get("buildEvent", {}) for item in invocations[0].get("event", [])]
        finishes = [event["finished"] for event in events if "finished" in event]
        if len(finishes) != 1:
            raise ValueError(arch + ": terminal event missing")
        finished = finishes[0]
        milliseconds = finished.get("finishTimeMillis")
        if milliseconds is None:
            milliseconds = round(datetime.fromisoformat(finished["finishTime"]).timestamp() * 1000)
        if int(milliseconds) != original["finish_ms"]:
            raise ValueError(arch + ": original finish identity mismatch")
        row["finish_ms"] = int(milliseconds)
        for target, digest in original["actions"].items():
            data = rpc("GetExecution", {"executionLookup": {"invocationId": invocation,
                "actionDigestHash": digest}, "inlineExecuteResponse": True})
            actions = [item for item in data.get("execution", []) if item.get("actionDigest", {}).get("hash") == digest]
            if len(actions) != 1:
                raise ValueError(target + ": expected one original action")
            action = actions[0]
            value = action.get("executeResponse", {}).get("result")
            if value is None or value.get("exitCode", 0) != original["exit"] or action.get("stage") not in (4, "COMPLETED"):
                raise ValueError(target + ": original terminal outcome mismatch")
            evidence = {"action_hash": digest, "exit_code": value.get("exitCode", 0), "streams": {}}
            row["actions"][target] = evidence
            for stream in ("stdout", "stderr"):
                content = base64.b64decode(value.get(stream + "Raw", ""))
                if not content and value.get(stream + "Digest"):
                    content = blob(value[stream + "Digest"], invocation)
                if content:
                    evidence["streams"][stream] = project(content)
        for event in events:
            target = event.get("id", {}).get("testResult", {}).get("label")
            if target not in original["actions"]:
                continue
            for output in event.get("testResult", {}).get("testActionOutput", []):
                if output.get("name") != "test.log":
                    continue
                uri = output.get("uri", "")
                match = re.fullmatch(r"bytestream://remote\.buildbuddy\.io/(?:[^?]*/)?blobs/([0-9a-f]{64})/(\d+)", uri)
                if not match:
                    continue
                row["test_logs"][target] = project(blob({"hash": match[1], "sizeBytes": match[2]}, invocation))
        for target, evidence in row["actions"].items():
            diagnostics = [line for stream in evidence["streams"].values() for line in stream["diagnostic_lines"]]
            diagnostics += row["test_logs"].get(target, {}).get("diagnostic_lines", [])
            if not diagnostics:
                raise ValueError(target + ": original diagnostic text unavailable")
        row["status"] = "RECOVERED"
    result["status"] = "PASS"
except Exception as err:
    result["error"] = str(err).replace(key, "[REDACTED]")[:1000]
finally:
    (Path(os.environ["RUNNER_TEMP"]) / "runtime-diagnostic.json").write_text(json.dumps(result, indent=2) + "\n")
raise SystemExit(result["status"] != "PASS")
