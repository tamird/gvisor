"""Retain the first failed proc analysis disposition, without command lines."""

import base64
import collections
import datetime
import json
import os
from pathlib import Path
import re
import urllib.request

uid = "8d469725-6171-400c-b821-6e3cef78aa9a"
source = "28cfe8fb884b83198704dd55a9ee53ac596b59fe"
key = os.environ["BUILDBUDDY_API_KEY"]
def rpc(method, body):
    req = urllib.request.Request("https://app.buildbuddy.io/rpc/BuildBuddyService/" + method,
        data=json.dumps(body).encode(), headers={"Content-Type":"application/json", "x-buildbuddy-api-key":key}, method="POST")
    with urllib.request.urlopen(req, timeout=35) as response:
        return json.load(response)
codes = re.compile(r"RESOURCE_EXHAUSTED|DEADLINE_EXCEEDED|UNAVAILABLE|CANCELLED|CANCELED|ABORTED|INTERNAL|timed out|deadline exceeded|out of memory|no registered executors|no available executors|context canceled|worker lost|exceeded [a-z ]+limit|connection reset|stream terminated|execution timeout", re.I)
record = {"invocation":uid,"source":source,"originalRunnerExit":34,"failedTarget":"//pkg/sentry/fsimpl/proc:proc","exec":[]}
for label in ("//pkg/sentry/fsimpl/proc:proc","@@//pkg/sentry/fsimpl/proc:proc"):
    try:
        rows = rpc("GetExecution",{"executionLookup":{"invocationId":uid,"targetLabel":label}}).get("execution",[])
        for row in rows:
            meta = row.get("executedActionMetadata",{})
            stat = row.get("status",{})
            rec = {"label":row.get("targetLabel"),"mnemonic":row.get("actionMnemonic"),
                "stage":row.get("stage"),"status":stat.get("code",0),"exit":row.get("exitCode",0),
                "codes":codes.findall(stat.get("message","")),"times":{k:v for k,v in meta.items() if k.endswith("Timestamp")}}
            record["exec"].append(rec)
    except Exception as exc:
        record["execLookupError"]=type(exc).__name__
try:
    raw=rpc("GetEventLogChunk",{"invocationId":uid,"minLines":100})
    lines=base64.b64decode(raw.get("buffer","")).decode(errors="replace").splitlines()
    record["retainedLogSignals"] = dict(collections.Counter(m.lower() for line in lines for m in codes.findall(line)))
    record["procFailureSeen"] = any("fsimpl/proc:proc" in line and "failed" in line.lower() for line in lines)
except Exception as exc:
    record["logLookupError"] = type(exc).__name__
data=json.dumps(record, indent=2)
assert key and key not in data
(Path(os.environ["RUNNER_TEMP"])/"records"/"original-proc.json").write_text(data)
