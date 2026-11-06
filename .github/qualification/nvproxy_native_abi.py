"""Read the actual per-driver outcomes, metadata and absence evidence from Bazel."""
from __future__ import annotations
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from urllib.parse import unquote, urlparse

tmp = Path(os.environ["RUNNER_TEMP"])
abi = "//pkg/sentry/devices/nvproxy:nvproxy_driver_abi_test"
version = r"\d{3}\.\d{2,3}\.\d{2}"
source = ast.parse(Path("tools/nvidia_driver_differ/driver_pins.bzl").read_text())
pins = ast.literal_eval(next(stmt.value for stmt in source.body if isinstance(stmt,ast.Assign)))
assert len(pins)==34 and sum(value is not None for value in pins.values())==25
assert all(re.fullmatch(version,k) for k in pins)
r = {"product":"b4929480061e95c901f3f21ed3ced64a8ea3f447","source":"f2a65e06ac211813a61ec1efadb8eeb6e6270e13","targets":{},"tests":{},"actions":[],
     "verified":False}
dest = tmp/"nvproxy-offline.json"
def save():
    dest.write_text(json.dumps(r,sort_keys=True))
def error_terms(text):
    known = ("no such file or directory", "unrecognized option", "permission denied",
             "not found", "error evaluating", "failed to fetch", "out of memory", "no space",
             "undefined reference", "checksum", "not in runfiles", "reading nvidia_driver_sources",
             "invalid label", "ambiguous", "unable to load", "build interrupted", "java heap space")
    return [term for term in known if term in text.lower()]
def first_errors(path):
    if not path.is_file():
        return []
    with path.open(errors="replace") as stream:
        return sorted({term for line in stream for term in error_terms(line)})
save()
r["first_errors"]=first_errors(tmp/"source-presubmit.log")
save()
bep=tmp/"source-presubmit.bep"
if bep.is_file():
    for line in bep.open():
        e=json.loads(line);i=e["id"]
        if "started" in e:r["invocation"]=str(uuid.UUID(e["started"]["uuid"]))
        if "finished" in e:r["exit"]=e["finished"].get("exitCode",{}).get("code",0)
        c=i.get("targetCompleted",{});owner=c.get("label")
        if owner == abi and not c.get("aspect"):r["targets"][owner]=e.get("completed",{}).get("success",False)
        a=e.get("action",{});tgt=i.get("actionCompleted",{})
        if a and not a.get("success") and len(r["actions"])<12:
            uri=a.get("stderr",{}).get("uri",""); cas=re.search(r"/blobs/([a-f0-9]{64})/(\d+)$",uri)
            r["actions"].append({"abi_owner":tgt.get("label")==abi,"kind":a.get("type") if a.get("type") in {"CppCompile","CppLink","GoCompilePkg","GoLink","TestRunner","Symlink"} else "other","stderr":[cas[1],int(cas[2])] if cas else None})
        owner=i.get("testResult",{}).get("label");t=e.get("testResult")
        if owner != abi or not t:continue
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
        known_cases = {"TestStructDefinitionParity"} | {"TestStructDefinitionParity/" + v for v in pins}
        row["cases"]={name:st for st,name in re.findall(r"--- (PASS|SKIP|FAIL): (Test[A-Za-z0-9_./-]+)",s) if name in known_cases}
        row["errors"]=error_terms(s)
        row["missing_reasons"]=[v for v in sorted(pins) if f"driver has no public source tag: {v}" in s]
        save()
save()
a=r["tests"].get(abi,{})
actual=a.get("cases",{})
r["expected"]={v:("PASS" if val else "SKIP") for v,val in pins.items()}
r["verified"]=(r.get("exit")==0 and len(r["targets"])==1 and all(r["targets"].values()) and len(r["tests"])==1 and
    all(t.get("status")=="PASSED" and t.get("no_route") and t.get("remote") and not t.get("cache") for t in r["tests"].values()) and
    actual.get("TestStructDefinitionParity")=="PASS" and all(actual.get("TestStructDefinitionParity/"+v)==expected for v,expected in r["expected"].items()) and
    set(a.get("missing_reasons",[]))=={v for v,val in pins.items() if not val})
save()
