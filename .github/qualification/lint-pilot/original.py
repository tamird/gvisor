"""Fixed-host original-only readback. Publish only known diagnostic categories."""

from hashlib import sha256
from http.client import HTTPSConnection, HTTPException
import json
import os
from pathlib import Path
import re

KEY = os.environ.pop("ORIGINAL_KEY")
ORIGINALS = {"local-config":"84ebe6ec-79aa-46dd-b59e-f170530ab102"}
MARKERS = ("bad formatting.bzl", "module-docstring", "# reformat",
           "docstring", "CPPLINT.cfg", "BuildifierCheck")


def post(endpoint, payload):
    conn=HTTPSConnection("app.buildbuddy.io",timeout=25)
    try:
        assert endpoint in ("/api/v1/GetLog","/rpc/BuildBuddyService/GetExecution")
        conn.request("POST",endpoint,json.dumps(payload),{"Content-Type":"application/json","x-buildbuddy-api-key":KEY})
        with conn.getresponse() as response:
            buf=response.read(16*1024*1024+1)
            if response.status!=200 or len(buf)>16*1024*1024:
                return {"read_status":response.status, "body_bytes":len(buf)}
            return json.loads(buf)
    finally:
        conn.close()


def facts(text):
    return {"fixture_messages": [line[line.index("test/util/.lint_pilot_control/"):][:260] for line in text.splitlines() if "test/util/.lint_pilot_control/" in line and ("# reformat" in line or "module-docstring" in line) and "https:" not in line and len(line) < 1024][:8], "bytes":len(text.encode()),"sha256":sha256(text.encode()).hexdigest(),
            "markers":[m for m in MARKERS if m.lower() in text.lower()],
            "source_lines":sorted(set(re.findall(r"(?:tools/lint_sources[.]bzl|tools/lint/defs[.]bzl|tools/lint/BUILD|MODULE[.]bazel):[0-9]+(?::[0-9]+)?",text)))}


def read(kind,uuid):
    row={"invocation":uuid,"pages":[]}
    payload={"selector":{"invocationId":uuid}}
    for _ in range(12):
        r=post("/api/v1/GetLog",payload)
        if "read_status" in r:
            row["read_status"]=r
            break
        row["pages"].append(facts(r.get("log",{}).get("contents","")))
        token=r.get("nextPageToken")
        if not token:break
        payload["pageToken"]=token
    if kind=="supplier":
        e=post("/rpc/BuildBuddyService/GetExecution",{"executionLookup":{"invocationId":uuid,"targetLabel":"//:clean"},"inlineExecuteResponse":True})
        row["executions"]=[]
        for a in e.get("execution",[]):
            if a.get("targetLabel")!="//:clean":continue
            response=a.get("executeResponse",{})
            if a.get("actionMnemonic")!="TestRunner" and "test-setup.sh" not in a.get("commandSnippet","") and "generate-xml" not in a.get("commandSnippet",""):
                continue
            row["executions"].append({"exit":a.get("exitCode",0),"snippet":facts(a.get("commandSnippet","")),
                "status":a.get("status",{}).get("code",0),"message":facts(a.get("status",{}).get("message","")),
                "execute_status":response.get("status",{}).get("code",0),
                "execute_message":facts(response.get("status",{}).get("message","")),
                "response_message":facts(response.get("message",""))})
    return row


out={}
for kind,uuid in ORIGINALS.items():
    try:out[kind]=read(kind,uuid)
    except (ValueError, KeyError, OSError, HTTPException) as e:out[kind]={"invocation":uuid,"read_error":type(e).__name__}
(Path(os.environ["RUNNER_TEMP"])/"original-launch.json").write_text(json.dumps(out,sort_keys=True,indent=2)+"\n")
