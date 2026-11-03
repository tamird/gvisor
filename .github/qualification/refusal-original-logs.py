"""Read the original two BuildBuddy invocations; do not run Bazel."""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
from cas_inputs import fetch, rpc

ROWS = {}
ART = Path(os.environ["RUNNER_TEMP"]) / "refusal-service-evidence.json"
for arm, uid in (("native", "2ca64aee-167b-4ebf-b916-9d2dd5c504e2"), ("stock", "28471d23-071a-4eff-8af0-5f6a754474d3")):
    row = ROWS[arm] = {"id": uid}
    try:
        data = json.loads(fetch("api/v1/GetInvocation", {"selector":{"invocation_id":uid}}))
        iv, = data["invocation"]
        for k in ("id","success","durationUsec","invocationStatus","exitCode","createdAtUsec","updatedAtUsec"):
            if k in iv: row[k] =iv[k]
    except Exception as exc:
        row["invocation_error"] = type(exc).__name__ + ": " + str(exc)[:180]
    try:
        token = ""
        log = []
        for _ in range(8):
            data=json.loads(fetch("api/v1/GetLog", {"selector":{"invocation_id":uid}, "page_token":token}))
            log.extend(data.get("log",{}).get("contents","").splitlines())
            token=data.get("nextPageToken",data.get("next_page_token",""))
            if not token:break
        row["truncated"] = bool(token)
        row["line_count"] = len(log)
        entries=[]
        for l in log:
            l=re.sub(r'\x1b\[[0-9;]*[A-Za-z]','',l)
            if not l.strip() or re.search("(?i)credential|token|authorization|key=|_header|secret|bearer|password|https?://[^/ ]+@",l):continue
            l=re.sub(r'https?://[^ ]+','<url>',l)
            entries.append(l.strip()[:460])
        errors=[x for x in entries if re.search(r'(?i)error|fail|Network is unreachable|timeout|timed out|Another command|denied|no space|still trying|waiting for|retrying|Killed|signal',x)]
        row["first"] = list(dict.fromkeys(entries))[:4]
        row["errors"] = list(dict.fromkeys(errors))[:12]
        row["last"] = list(dict.fromkeys(entries))[-15:]
    except Exception as exc:
        row["log_error"] = type(exc).__name__ + ": " + str(exc)[:180]
    try:
        response=rpc("GetExecution",{"executionLookup":{"invocationId":uid}})
        xs=response.get("execution",[])
        row["execution_count"]=len(xs);row["execution_status"]={}
        for x in xs:
            k=str((x.get("stage"),x.get("status",{}).get("code",0),x.get("exitCode",0)))
            row["execution_status"][k]=row["execution_status"].get(k,0)+1
        row["example_actions"]=[{k:x[k] for k in ("actionMnemonic","targetLabel","stage","exitCode","actionDigest") if k in x} for x in xs[-4:]]
    except Exception as exc:
        row["execution_error"]=type(exc).__name__+": "+str(exc)[:180]
out={"original_artifact":"9678bdde5f9197a2130051cd2289211283eb2e1089470f90ac27d79bd31118af","rows":ROWS,"reexecuted":0,"finished":datetime.now(timezone.utc).isoformat()}
b=json.dumps(out,indent=2).encode();assert os.environ["BUILDBUDDY_API_KEY"].encode() not in b and not re.search(rb'gh[pousr]_[A-Za-z0-9]|github_pat_|Authorization:|x-buildbuddy-api-key',b,re.I);ART.write_bytes(b)
