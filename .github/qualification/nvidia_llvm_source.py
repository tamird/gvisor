"""Project the original four NVIDIA target receipts, including driver-source skips."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlparse

tmp=Path(os.environ["RUNNER_TEMP"])
source="a66f27a435cb5a14fcfbfcfbe7c3b0fa390a40d7"
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==source
test_names={"//tools/nvidia_driver_differ:driver_ast_parser_test":"TestParser",
            "//pkg/sentry/devices/nvproxy:nvproxy_driver_parity_test":"TestStructDefinitionParity"}
bin_dirs={"//tools/nvidia_driver_differ:driver_ast_parser":"tools/nvidia_driver_differ/driver_ast_parser",
          "//tools/nvidia_driver_differ:run_differ":"tools/nvidia_driver_differ/run_differ_/run_differ"}
owners=set(test_names) | set(bin_dirs)
assert not (tmp/"source-presubmit.bazelrc").exists()
r={"source":source,"product":"7a246d406ef1a71027158fa85e4489b290852f55","targets":{},"tests":{},"elf":{},"failed_actions":[],
   "verified":False,"coordinator":json.loads((tmp/"source-presubmit-memory.json").read_text())}
def save():
    (tmp/"nvidia-llvm-owners.json").write_text(json.dumps(r,separators=(",",":")))
save()
bep=tmp/"source-presubmit.bep"
if bep.is_file():
    for line in bep.open():
        e=json.loads(line); i=e["id"]
        if "started" in e and re.fullmatch("[a-f0-9-]{36}",e["started"].get("uuid","")):
            r["invocation"]=e["started"]["uuid"]
        if "finished" in e: r["exit"]=e["finished"].get("exitCode",{}).get("code",0)
        c=i.get("targetCompleted",{}); owner=c.get("label")
        if owner in owners and not c.get("aspect"):
            r["targets"][owner]=e.get("completed",{}).get("success",False)
        action=e.get("action")
        if action and not action.get("success") and len(r["failed_actions"])<12:
            target=i.get("actionCompleted",{}).get("label","")
            uri=action.get("stderr",{}).get("uri","")
            m=re.search(r"/blobs/([a-f0-9]{64})/(\d+)$",uri)
            r["failed_actions"].append({"owner":target if target in owners else hashlib.sha256(target.encode()).hexdigest(),
                                      "kind":action["type"] if action["type"] in {"CppCompile","CppLink","GoLink","GoCompilePkg"} else "other",
                                      "stderr": [m[1],int(m[2])] if m else None})
        owner=i.get("testResult",{}).get("label"); t=e.get("testResult")
        if owner in test_names and t:
            info=t.get("executionInfo",{}); row={"status":t["status"],"remote":info.get("strategy")=="remote",
                "cached":bool(t.get("cachedLocally") or info.get("cachedRemotely")),"cases":[]}
            r["tests"][owner]=row
            log=next((o for o in t.get("testActionOutput",[]) if o["name"]=="test.log"),None)
            if not log:continue
            u=urlparse(log["uri"])
            path=Path(unquote(u.path)) if u.scheme=="file" else Path("bazel-testlogs")/owner[2:].replace(":","/")/"test.log"
            if not path.is_file():continue
            data=path.read_bytes(); sha=hashlib.sha256(data).hexdigest()
            if u.scheme=="bytestream":
                assert u.path.endswith("/"+sha+"/"+str(len(data)))
            row["log"]=sha
            row["cases"]=[[st.decode(),n.decode()] for st,n in re.findall(
                rb"--- (PASS|FAIL|SKIP): (Test(?:Parser|StructDefinitionParity|DriverChecksums)[A-Za-z0-9_./-]{0,130}) ",data)][:4000]
            row["driver_source_skips"]=len(re.findall(rb"Skipping parity test for [0-9.]+ because driver source is not available",data))
    save()
for owner,path in bin_dirs.items():
    file=Path("bazel-bin")/path
    if not file.is_file() or not r["targets"].get(owner):continue
    with file.open("rb") as f:
        head=f.read(64); f.seek(0); sha=hashlib.file_digest(f,"sha256").hexdigest()
    r["elf"][owner]={"bytes":file.stat().st_size,"sha256":sha,"amd64":head[:6]==b"\x7fELF\x02\x01" and head[18:20]==b"\x3e\x00"}
save()
r["verified"]=(r.get("exit")==0 and set(r["targets"])==owners and all(r["targets"].values()) and set(r["elf"])==set(bin_dirs)
    and all(v["amd64"] for v in r["elf"].values()) and set(r["tests"])==set(test_names) and
    all(v["status"]=="PASSED" and v["remote"] and not v["cached"] and ["PASS",test_names[k]] in v["cases"] for k,v in r["tests"].items()))
save()
