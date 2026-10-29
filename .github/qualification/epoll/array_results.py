"""Classify missing/failed owners using the original actions, without rebuilding."""
import base64
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

from original_worker import first, rpc, safe

normal = "//pkg/state/tests:tests_test"
arm = ["//pkg/state:state_nogo", "//pkg/state:state_test_nogo", "//pkg/state/pretty:pretty_nogo",
       "//pkg/state/wire:wire_nogo","//pkg/state/wire:wire_test_nogo", "//pkg/state/tests:tests_nogo","//pkg/state/tests:tests_test_nogo"]
lanes = [("normal","763b71d9-ad3b-4cfb-ad9e-3a60f58274c6",[normal]),
         ("race","b8394d1e-566a-4e30-9926-8c1d82d30364",[normal]),
         ("arm","7b01c368-7e80-41b9-9007-9d06ac683806",arm)]
pat=re.compile(r"exec format|cannot execute|no such file|timeout|timed out|deadline|killed|resource exhausted|out of memory|no space left|worker.*(lost|disconnect)|remote execution failure|panic:|fatal:|SIG(QUIT|TERM)|incompatible binary|WARN.*oom|^=== RUN |^--- (PASS|FAIL|SKIP):|detected race|goroutine [0-9]+ \[|/pkg/state/tests/|^ERROR:",re.I)
def rows(text):
    return list(dict.fromkeys(safe(line) for line in text.splitlines() if pat.search(line))) [-24:]
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args): return None

def fetch(inv,d):
    h=d['hash'];n=int(first(d,"sizeBytes","size_bytes"))
    assert re.fullmatch("[0-9a-f]{64}",h) and 0<=n<10_000_000
    url="https://app.buildbuddy.io/file/download?"+urllib.parse.urlencode({"invocation_id":inv,"bytestream_url":f"bytestream://remote.buildbuddy.io/blobs/{h}/{n}"})
    req=urllib.request.Request(url,headers={"x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"]})
    with urllib.request.build_opener(NoRedirect()).open(req,timeout=20) as f:data=f.read(n+1)
    assert len(data)==n and hashlib.sha256(data).hexdigest()==h
    return data.decode(errors="replace")
records={}
for lane,inv,targets in lanes:
    out=records[lane]={"invocation":inv,"test_actions":[],"event_diagnostics":[]}
    for label in targets:
        r=rpc("GetExecution",{"executionLookup":{"invocationId":inv,"targetLabel":label},"inlineExecuteResponse":True})
        relevant=[ex for ex in r.get("execution",[]) if ex.get("actionMnemonic")=="TestRunner"]
        if not relevant:
            r=rpc("GetExecution",{"executionLookup":{"invocationId":inv,"targetLabel":"@@"+label},"inlineExecuteResponse":True})
            relevant=[ex for ex in r.get("execution",[]) if ex.get("actionMnemonic")=="TestRunner"]
        for ex in relevant:
            row={"label":label,"status":ex.get("status",{}).get("code",0),"message":safe(ex.get("status",{}).get("message","")),"exit":ex.get("exitCode",0),"action":ex.get("actionDigest"),"files":{}}
            result=(ex.get("executeResponse") or {}).get("result",{})
            filelist={item["path"]:item["digest"] for item in result.get("outputFiles",[]) if item["path"].endswith("/test.log")}
            for key in ["stderr","stdout"]:
                if key+"Digest" in result:filelist[key]=result[key+"Digest"]
            for path,digest in filelist.items():
                try:
                    trace=fetch(inv,digest); lines=rows(trace)
                    if path.endswith("test.log") and not lines:
                        lines=[safe(line) for line in trace.splitlines() if line][:5]
                    row["files"][path]={"digest":digest,"findings":lines}
                except Exception as exc:row["files"][path]={"digest":digest,"error":safe(str(exc))}
            out["test_actions"].append(row)
    chunk=""
    for _ in range(5):
        r=rpc("GetEventLogChunk",{"invocationId":inv,"chunkId":chunk,"minLines":1500})
        log=base64.b64decode(r.get("buffer","")).decode(errors="replace")
        out["event_diagnostics"]+=rows(log)
        chunk=first(r,"previousChunkId","previous_chunk_id")
        if not chunk or out["event_diagnostics"]:break
print(json.dumps({k:{"actions":len(v["test_actions"]),"logs":len(v["event_diagnostics"])} for k,v in records.items()}))
Path("array-original.json").write_text(json.dumps(records,indent=2)+"\n")
