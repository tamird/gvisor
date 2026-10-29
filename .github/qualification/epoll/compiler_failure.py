"""Recover diagnostics only for an original compiler action; no compilation."""
import base64
import json
import re
from pathlib import Path

from original_worker import first, rpc, safe

invocation = "f68018e9-aaa6-4258-a9a0-48b990d14f23"
out = {"invocation": invocation, "failed_actions": [], "stderr": [], "log_diagnostics": []}
diagnostic = re.compile(r"fatal error:|(?:[ .:\w/-]+):[0-9]+(?::[0-9]+)?: error:|internal compiler error|cc1plus: (?:error|warning):|compilation terminated|out of memory|killed signal|disk quota exceeded|no space left", re.I)

def add(text, key):
    for line in text.splitlines():
        if diagnostic.search(line):
            line = safe(line)
            if line not in out[key] and len(out[key]) < 30:
                out[key].append(line)

for target in ("@@protobuf+//src/google/protobuf:protobuf", "@protobuf//src/google/protobuf:protobuf"):
    r = rpc("GetExecution", {"executionLookup": {"invocationId": invocation, "targetLabel": target}})
    for ex in r.get("execution", []):
        if first(ex, "actionMnemonic", "action_mnemonic") != "CppCompile" or not (first(ex,"exitCode","exit_code") or ex.get("status",{}).get("code")):
            continue
        digest = first(ex, "actionDigest", "action_digest")
        row = {"target": target, "digest": digest, "exit": first(ex,"exitCode","exit_code")}
        if "status" in ex:
            row["status"] = safe(ex["status"].get("message",""))
        # Request the accepted failing action only, excluding successful siblings.
        rr = rpc("GetExecution",{"executionLookup":{"invocationId":invocation,"actionDigestHash":digest["hash"]},"inlineExecuteResponse":True})
        for found in rr.get("execution",[]):
            response=first(found,"executeResponse","execute_response") or {}
            result=response.get("result",{})
            for channel in ("stdout","stderr"):
                raw=first(result,channel+"Raw",channel+"_raw")
                if raw:
                    add(base64.b64decode(raw).decode(errors="replace"),"stderr")
                value=first(result,channel+"Digest",channel+"_digest")
                if value:
                    row[channel+"_digest"]=value
        out["failed_actions"].append(row)
    if out["failed_actions"]:
        break
chunk=""
for _ in range(12):
    r=rpc("GetEventLogChunk",{"invocationId":invocation,"chunkId":chunk,"minLines":1000})
    if "requestError" in r:
        out["log_error"]=r["requestError"]; break
    data=base64.b64decode(r.get("buffer","")).decode(errors="replace")
    add(data,"log_diagnostics")
    chunk=first(r,"previousChunkId","previous_chunk_id")
    if out["log_diagnostics"] or not chunk:
        break
print(json.dumps(out,indent=2))
Path("original-compiler-diagnostic.json").write_text(json.dumps(out,indent=2)+"\n")
