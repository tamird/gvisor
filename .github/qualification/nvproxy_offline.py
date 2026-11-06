"""Read the actual per-driver outcomes, metadata and absence evidence from Bazel."""
from __future__ import annotations
import ast
import collections
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

tmp = Path(os.environ["RUNNER_TEMP"])
abi = "//pkg/sentry/devices/nvproxy:nvproxy_driver_abi_test"
updater = "//tools/nvidia_driver_differ/sources:update_test"
manifest = "//tools/nvidia_driver_differ:driver_abi"
expected_cases = {"TestTagCommits","TestPinSource","TestReplacePinsPreservesOtherDeclarations"}
version = r"\d{3}\.\d{2,3}\.\d{2}"
source = ast.parse(Path("tools/nvidia_driver_differ/driver_sources.bzl").read_text())
pins = ast.literal_eval(next(stmt.value for stmt in source.body if isinstance(stmt,ast.Assign)))
assert len(pins)==34 and sum(value is not None for value in pins.values())==25
assert all(re.fullmatch(version,k) for k in pins)
r = {"product":"ed92709ded197270c492cc0917f220c96e6df718","source":"fa5e09a762a2bb2ba22e22713e553bd3a0c2f5f3","targets":{},"tests":{},"actions":[],
     "definitions":{}, "verified":False}
dest = tmp/"nvproxy-offline.json"
def save():
    dest.write_text(json.dumps(r,sort_keys=True))
def public_error(text):
    if re.search(r"(?i)token|authorization|remote.header|cookie|api.key|secret|gh[pousr]_",text):
        return "[redacted]"
    return re.sub(r"(?:https?://\S+|/home/runner/[^\s:]+|[A-Za-z0-9_+./-]{80,})","[path]",text[:460])
def first_errors(path):
    if not path.is_file():return []
    with path.open(errors="replace") as f:
        return [public_error(line.strip()) for line in f if line.startswith("ERROR:") or line.startswith("Error in " )][:4]
save()
r["first_errors"]=first_errors(tmp/"source-presubmit.log")
r["updater_exit"]=(tmp/"updater-exit").read_text().strip() if (tmp/"updater-exit").exists() else None
r["generated_identical"]=(tmp/"updater-diff-exit").read_text().strip()=="0" if (tmp/"updater-diff-exit").exists() else False
r["updater_errors"]=first_errors(tmp/"updater.log")
up_log=tmp/"updater.log"
r["updater_versions"]={}
if up_log.exists():
    for line in up_log.read_text(errors="replace").splitlines():
        m=re.fullmatch(f"({version}): ([0-9a-f]{{40}}|no public source version)",line)
        if m and m[1] in pins:r["updater_versions"][m[1]]=m[2]
if (tmp/"original-nvidia-tls.json").is_file():
    r["original_checksum_log"]=json.loads((tmp/"original-nvidia-tls.json").read_text())
save()
bep=tmp/"source-presubmit.bep"
if bep.is_file():
    for line in bep.open():
        e=json.loads(line);i=e["id"]
        if "started" in e:r["invocation"]=e["started"].get("uuid","")
        if "finished" in e:r["exit"]=e["finished"].get("exitCode",{}).get("code",0)
        c=i.get("targetCompleted",{});owner=c.get("label")
        if owner in {abi,updater,manifest} and not c.get("aspect"):r["targets"][owner]=e.get("completed",{}).get("success",False)
        a=e.get("action",{});tgt=i.get("actionCompleted",{})
        if a and not a.get("success") and len(r["actions"])<12:
            uri=a.get("stderr",{}).get("uri",""); cas=re.search(r"/blobs/([a-f0-9]{64})/(\d+)$",uri)
            r["actions"].append({"target":public_error(tgt.get("label","")),"kind":a.get("type",""),"stderr":[cas[1],int(cas[2])] if cas else None})
        owner=i.get("testResult",{}).get("label");t=e.get("testResult")
        if owner not in {abi,updater} or not t:continue
        info=t.get("executionInfo",{});row={"status":t["status"],"remote":info.get("strategy")=="remote","cache":bool(t.get("cachedLocally") or info.get("cachedRemotely"))}
        r["tests"][owner]=row
        log=next((x for x in t.get("testActionOutput",[]) if x["name"]=="test.log"),None)
        if not log:continue
        u=urlparse(log["uri"]);path=Path(unquote(u.path)) if u.scheme=="file" else Path("bazel-testlogs")/owner[2:].replace(":","/")/"test.log"
        if not path.is_file():continue
        b=path.read_bytes();sha=hashlib.sha256(b).hexdigest()
        if u.scheme=="bytestream":assert u.path.endswith("/"+sha+"/"+str(len(b)))
        row["log"]=[sha,len(b)];s=b.decode(errors="replace")
        row["no_route"]="offline worker: route rejected with ENETUNREACH" in s
        row["cases"]={name:st for st,name in re.findall(r"--- (PASS|SKIP|FAIL): (Test[A-Za-z0-9_./-]+)",s)}
        row["errors"]=[public_error(l.strip()) for l in s.splitlines() if re.search(r"\.go:\d+:|^panic:",l) and "no public source tag" not in l][:6]
        row["missing_reasons"]=[v for v in sorted(pins) if f"driver {v} has no public source tag in the generated source index" in s]
        save()
base=Path("bazel-bin/tools/nvidia_driver_differ")
f=base/"driver_abi.json"
if f.is_file():
    fields=json.loads(f.read_text())
    for ver,target in fields.items():
        assert ver in pins
        if target is None:
            r["definitions"][ver]=None;continue
        assert target==f"tools/nvidia_driver_differ/driver_abi_{ver.replace('.','_')}.json"
        p=Path("bazel-bin")/target
        if p.is_file():
            data=p.read_bytes();d=json.loads(data)
            r["definitions"][ver]={"sha256":hashlib.sha256(data).hexdigest(),"bytes":len(data),
                                   "records":len(d.get("records",d.get("Records",{}))),"constants":len(d.get("constants",d.get("Constants",{})))}
save()
a=r["tests"].get(abi,{});u=r["tests"].get(updater,{})
actual=a.get("cases",{})
r["verified"]=(r.get("exit")==0 and len(r["targets"])==3 and all(r["targets"].values()) and len(r["tests"])==2 and
    all(t.get("status")=="PASSED" and t.get("no_route") and t.get("remote") and not t.get("cache") for t in r["tests"].values()) and
    actual.get("TestStructDefinitionParity")=="PASS" and all(actual.get("TestStructDefinitionParity/"+v)==("PASS" if val else "SKIP") for v,val in pins.items()) and
    set(a.get("missing_reasons",[]))=={v for v,val in pins.items() if not val} and all(u.get("cases",{}).get(n)=="PASS" for n in expected_cases) and
    set(r["definitions"])==set(pins) and all((r["definitions"][v] and r["definitions"][v]["records"] and r["definitions"][v]["constants"]) if val else r["definitions"][v] is None for v,val in pins.items()) and
    r["generated_identical"] and r["updater_exit"]=="0" and set(r["updater_versions"])==set(pins))
save()
