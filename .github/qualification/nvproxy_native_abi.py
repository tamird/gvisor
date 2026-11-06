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
parser = "//tools/nvidia_driver_differ:driver_ast_parser_test"
version = r"\d{3}\.\d{2,3}\.\d{2}"
source = ast.parse(Path("tools/nvidia_driver_differ/driver_sources.bzl").read_text())
pins = ast.literal_eval(next(stmt.value for stmt in source.body if isinstance(stmt,ast.Assign)))
assert len(pins)==34 and sum(value is not None for value in pins.values())==25
assert all(re.fullmatch(version,k) for k in pins)
r = {"product":"a2a4bd4b24213a8331f301c67020cb378a059d74","source":"b11561ccd3e5bd078b5542a2ee5cdde0a4d36a85","targets":{},"tests":{},"actions":[],
     "verified":False}
dest = tmp/"nvproxy-offline.json"
def save():
    dest.write_text(json.dumps(r,sort_keys=True))
def public_error(text):
    if re.search(r"(?i)token|authorization|remote.header|cookie|api.key|secret|gh[pousr]_",text):
        return "[redacted]"
    return re.sub(r"(?:https?://\S+|/home/runner/[^\s:]+|[A-Za-z0-9_+./-]{80,})","[path]",text[:460])
def first_errors(path):
    if not path.is_file():return []
    found=[]
    with path.open(errors="replace") as f:
        for line in f:
            if (line.startswith(("ERROR:","Error in ")) or re.search(r"(?:\.go|\.cc|\.bzl):[0-9]+:|fatal error:",line)) and len(found)<8:
                found.append(public_error(line.strip()))
    return found
save()
r["first_errors"]=first_errors(tmp/"source-presubmit.log")
save()
bep=tmp/"source-presubmit.bep"
if bep.is_file():
    for line in bep.open():
        e=json.loads(line);i=e["id"]
        if "started" in e:r["invocation"]=e["started"].get("uuid","")
        if "finished" in e:r["exit"]=e["finished"].get("exitCode",{}).get("code",0)
        c=i.get("targetCompleted",{});owner=c.get("label")
        if owner in {abi,parser} and not c.get("aspect"):r["targets"][owner]=e.get("completed",{}).get("success",False)
        a=e.get("action",{});tgt=i.get("actionCompleted",{})
        if a and not a.get("success") and len(r["actions"])<12:
            uri=a.get("stderr",{}).get("uri",""); cas=re.search(r"/blobs/([a-f0-9]{64})/(\d+)$",uri)
            r["actions"].append({"target":public_error(tgt.get("label","")),"kind":a.get("type",""),"stderr":[cas[1],int(cas[2])] if cas else None})
        owner=i.get("testResult",{}).get("label");t=e.get("testResult")
        if owner not in {abi,parser} or not t:continue
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
save()
a=r["tests"].get(abi,{});p=r["tests"].get(parser,{})
actual=a.get("cases",{})
r["expected"]={v:("PASS" if val else "SKIP") for v,val in pins.items()}
r["verified"]=(r.get("exit")==0 and len(r["targets"])==2 and all(r["targets"].values()) and len(r["tests"])==2 and
    all(t.get("status")=="PASSED" and t.get("no_route") and t.get("remote") and not t.get("cache") for t in r["tests"].values()) and
    actual.get("TestStructDefinitionParity")=="PASS" and all(actual.get("TestStructDefinitionParity/"+v)==expected for v,expected in r["expected"].items()) and
    set(a.get("missing_reasons",[]))=={v for v,val in pins.items() if not val} and p.get("cases",{}).get("TestParser")=="PASS")
save()
