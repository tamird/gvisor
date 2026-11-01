"""Recover the original pkg/abi/linux analysis disposition without rerunning it."""

import base64
import json
import os
from pathlib import Path
import re
import urllib.request

IDS = {"amd64": "453528d0-92c4-4f90-b578-256e054326e7", "arm64": "679948ec-6ae6-472d-b0e7-c3fca55ad760"}
KEY = os.environ["BUILDBUDDY_API_KEY"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def rpc(method, body):
    req = urllib.request.Request("https://app.buildbuddy.io/rpc/BuildBuddyService/" + method, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-buildbuddy-api-key": KEY})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=35) as response:
        return json.load(response)


uid = IDS[os.environ["ARCH"]]
label = "//pkg/abi/linux:linux"
signals = re.compile(r"RESOURCE_?EXHAUSTED|DEADLINE_?EXCEEDED|UNAVAILABLE|CANCELLED|CANCELED|ABORTED|INTERNAL|timed out|deadline exceeded|out of memory|no registered executors|no available executors|context canceled|worker lost|connection reset|execution timeout|action timeout|preempted|exceeded [a-z ]*duration of [\dsmh.]+|timeout of [\dsmh.]+|exceeded [\d.]+[smh]|SIGKILL|signal: killed", re.I)
record = {"invocation": uid, "label": label, "exit": 34, "executions": []}
for spelling in (label, "@@" + label):
    try:
        result = rpc("GetExecution", {"executionLookup": {"invocationId": uid, "targetLabel": spelling}})
        for row in result.get("execution", []):
            if row.get("targetLabel", "").removeprefix("@@") != label or row.get("actionMnemonic") != "GoStaticAnalysis":
                continue
            status = row.get("status", {})
            md = row.get("executedActionMetadata", {})
            record["executions"].append({"mnemonic": "GoStaticAnalysis", "stage": row.get("stage"), "code": status.get("code", 0),
                                         "exit": row.get("exitCode", 0), "action": row.get("actionDigest"),
                                         "signals": signals.findall(status.get("message", "")),
                                         "started": md.get("executionStartTimestamp"), "completed": md.get("executionCompletedTimestamp")})
    except Exception as err:
        record["execution_error"] = type(err).__name__
try:
    chunk = rpc("GetEventLogChunk", {"invocationId": uid, "minLines": 140})
    terminal = base64.b64decode(chunk.get("buffer", "")).decode(errors="replace")
    record["target_failure"] = any(label in line and "failed" in line for line in terminal.splitlines())
    record["log_signals"] = list(dict.fromkeys(signals.findall(terminal)))
except Exception as err:
    record["log_error"] = type(err).__name__
data = json.dumps(record, indent=2)
assert KEY and KEY not in data and "x-buildbuddy-api-key" not in data
(Path(os.environ["RUNNER_TEMP"]) / "bazel9/records/original-rpc.json").write_text(data)
print(data)
