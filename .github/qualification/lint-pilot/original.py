"""Fixed-host original-only readback. Publish only known diagnostic categories."""

from hashlib import sha256
from http.client import HTTPSConnection, HTTPException
import json
import os
from pathlib import Path
import re

KEY = os.environ.pop("ORIGINAL_KEY")
ORIGINALS = {"integrated_detection":"b27a5bb4-41ec-47ad-9115-90ff60595de3", "stock_waiter":"a7df6eb9-a593-4a9e-93cc-be4780e8739a"}


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
    return {"tags": sorted(set(re.findall(r'Using the "(go_deps[.][a-z_]+)" tag in a non-root Bazel module is forbidden', text))),
            "bytes": len(text.encode()), "sha256": sha256(text.encode()).hexdigest()}


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
    return row


out={}
for kind,uuid in ORIGINALS.items():
    try:out[kind]=read(kind,uuid)
    except (ValueError, KeyError, OSError, HTTPException) as e:out[kind]={"invocation":uuid,"read_error":type(e).__name__}
(Path(os.environ["RUNNER_TEMP"])/"original-launch.json").write_text(json.dumps(out,sort_keys=True,indent=2)+"\n")
