"""Read actual source inventories, downstream repository mappings and Bazel results."""
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT=Path(os.environ["GITHUB_WORKSPACE"])
TMP=Path(os.environ["RUNNER_TEMP"])/"nvproxy-dev"
CLIENT=TMP/"consumer"; CLIENT.mkdir(exist_ok=True)
OLD="efa9acfb4e2d91c5944c166a9f2269e0b3ba4322"
NEW="b39f1d18259f0ddb2cfa37516870e6d2b2babbd5"
PIN="tools/nvidia_driver_differ/driver_pins.bzl"
NAME="nvidia_driver_580_95_05"
CAT="tools/licensecheck/dependencies.yaml"
VERSION=r"\d{3}\.\d{2,3}\.\d{2}"
RESULT=TMP/"result.json"
report=json.loads(RESULT.read_text()) if RESULT.exists() else {"source":NEW,"parent":OLD,"arms":{}}
def save(): RESULT.write_text(json.dumps(report,sort_keys=True))
def sha(b):return hashlib.sha256(b).hexdigest()
def git(*args):return subprocess.check_output(["git",*args],cwd=ROOT,text=True).strip()
def public(s):
    return "[redacted]" if re.search(r"(?i)token|authorization|remote_header|cookie|api.key|secret",s) else s.strip()[:650]
def call(r,name,args,cwd=ROOT):
    op=TMP/(name+".out");err=TMP/(name+".err");start=time.monotonic()
    with op.open("wb") as o,err.open("wb") as e:
        cp=subprocess.run(args,cwd=cwd,stdout=o,stderr=e)
    a=op.read_bytes();b=err.read_bytes()
    msg=(b+b"\n"+a).decode(errors="replace")
    r[name]={"exit":cp.returncode,"stdout":[sha(a),len(a)],"stderr":[sha(b),len(b)],"s":round(time.monotonic()-start,2),
             "error":[public(s) for s in msg.splitlines() if re.search(r"^ERROR:|Error in |^Error:|missing entry for |stale entry for |only.*root module|not visible|no such package|fatal error:|undefined:|no such target|no repository visible",s)][:7]}
    save();return cp.returncode,a.decode(errors="replace"),msg
pins_src=ast.parse(git("show",NEW+":"+PIN));allpins=ast.literal_eval(next(n.value for n in pins_src.body if isinstance(n,ast.Assign)))
pins={"nvidia_driver_"+v.replace(".","_"):x for v,x in allpins.items() if x}
assert len(allpins)==34 and len(pins)==25
def inventory(r,arm):
    rc,s,_=call(r,"root-map",["bazel","mod","dump_repo_mapping",""])
    m=json.loads(s) if not rc else {}
    r["root_sources"]={k:v for k,v in m.items() if k.startswith("nvidia_driver_")}
    rc,s,_=call(r,"root-show",["bazel","mod","show_repo",*["@"+n for n in sorted(pins)]])
    observed={}
    for part in re.findall(r"(?ms)^http_archive\(\s*$(.*?)^\)\s*$",s):
        match=re.search(r"https://github.com/NVIDIA/open-gpu-kernel-modules/archive/([a-f0-9]{40})\.tar\.gz",part)
        digest=re.search(r'(?m)^\s*sha256\s*=\s*"([a-f0-9]{64})"',part)
        if match and digest and "open-gpu-kernel-modules-"+match[1] in part:
            for n,x in pins.items():
                if x[0]==match[1]:observed[n]=[match[1],digest[1]]
    r["root_archives"]=observed
    r["root_ok"]=not rc and set(r["root_sources"])==set(pins) and observed=={k:list(x) for k,x in pins.items()}
    save()
def tidy(r,arm):
    p=ROOT/"MODULE.bazel";before=p.read_text()
    for i in (1,2):
        n="tidy"+str(i);call(r,n,["bazel","mod","tidy"]);after=p.read_text()
        changes=[s[:280] for s in difflib.unified_diff(before.splitlines(),after.splitlines(),n=0) if s.startswith(("+","-")) and not s.startswith(("+++","---"))]
        r[n].update(input=sha(before.encode()),output=sha(after.encode()),changes=len(changes),driver_changes=[s for s in changes if "nvidia_driver_" in s],
                    other=[s for s in changes if "nvidia_driver_" not in s][:5],changes_sha=sha("\n".join(changes).encode()));save();before=after
    git("restore","--source=HEAD","--","MODULE.bazel")
def client(r,arm):
    (CLIENT/"MODULE.bazel").write_text('module(name = "consumer")\nbazel_dep(name = "gvisor", version = "0.0.1")\nlocal_path_override(module_name = "gvisor", path = '+json.dumps(str(ROOT))+')\n')
    (CLIENT/"BUILD.bazel").write_text("")
    rc,s,_=call(r,"client-root-map",["bazel","mod","dump_repo_mapping",""],CLIENT)
    canon=json.loads(s).get("gvisor") if not rc else None
    assert canon and re.fullmatch(r"gvisor[.+~a-zA-Z0-9_-]*",canon), "gvisor repository not in consumer mapping"
    rc,s,_=call(r,"dependency-map",["bazel","mod","dump_repo_mapping",canon],CLIENT)
    m=json.loads(s) if not rc else {}
    r["dependency_repo"]=canon;r["client_sources"]={k:v for k,v in m.items() if k.startswith("nvidia_driver_")};save()
    target="@gvisor//pkg/sentry/devices/nvproxy:nvproxy";bep=TMP/(arm+".bep")
    rc,_,_=call(r,"build-client",["timeout","-k","45s","22m","bazel","build","--jobs=4","--build_event_json_file="+str(bep),target],CLIENT)
    r["terminals"]=[]
    if bep.is_file():
        for ln in bep.open():
            e=json.loads(ln);o=e["id"].get("targetCompleted",{})
            if o.get("label","").endswith("//pkg/sentry/devices/nvproxy:nvproxy"):r["terminals"].append([o["label"],e.get("completed",{}).get("success",False)])
            if "started" in e:r["client_invocation"]=e["started"].get("uuid")
    r["client_built"]=not rc and any(v for _,v in r["terminals"]);save()
def formatting(r,arm):
    files=git("diff","--name-only",NEW+"^",NEW).splitlines()
    b=[f for f in files if f.endswith((".bzl",".BUILD")) or Path(f).name in {"BUILD","BUILD.bazel","MODULE.bazel"}]
    g=[f for f in files if f.endswith(".go")]
    rc,s,_=call(r,"gofmt",["gofmt","-l",*g]);r["gofmt_changed"]=s.splitlines()
    rc,s,msg=call(r,"buildifier",[str(TMP/"buildifier"),"--mode=check",*b]);r["buildifier_unformatted"]=[l for l in msg.splitlines() if any(x in l for x in b)][:10];save()
def generate(r,arm):
    p=ROOT/PIN;r["pin_runs"]=[]
    for i in (1,2):
        old=p.read_bytes();rc,_,msg=call(r,"generate"+str(i),["bazel","run","--jobs=4","//tools/nvidia_driver_differ/sources:update"])
        tags=dict(re.findall(r"(?m)^("+VERSION+"): ([a-f0-9]{40}|no public source version)$",msg))
        r["pin_runs"].append({"exit":rc,"before":sha(old),"after":sha(p.read_bytes()),"tags":tags,"dirty":git("diff","--name-only","HEAD").splitlines()});save()
def license_audit(r,arm):
    cat=(ROOT/CAT).read_text();rows={m[1]:m[0] for m in re.finditer(r"(?ms)^- dependency: ([^\n]+)\n.*?(?=^- dependency:|\Z)",cat)}
    r["catalog_sha_commit"]={n:all(z in rows.get(n,"") for z in x) for n,x in pins.items()}
    cmd=["bazel","run","--jobs=4","//tools/licensecheck/main:licensecheck","--","--mode=verify"]
    _,_,msg=call(r,"verify",cmd);m=re.search(r"all (\d+) dependencies have up-to-date license entries",msg);r["verified_entries"]=int(m[1]) if m else None
    neg=TMP/"missing.yaml";neg.write_text(cat.replace(rows[NAME],""))
    _,_,msg=call(r,"negative",[*cmd,"--yaml="+str(neg)]);r["actual_missing"]=re.findall(r"missing entry for ([a-zA-Z0-9_.+-]+)",msg);save()
def runfile_paths(r,arm):
    target="//pkg/sentry/devices/nvproxy:nvproxy_driver_abi_test"
    rc,s,_=call(r,"actual-abi-action",["bazel","aquery","--config=x86_64","--output=jsonproto","--include_artifacts=false",'mnemonic("TestRunner", '+target+')'])
    actions=json.loads(s).get("actions",[]) if not rc else []
    matches=[v["value"] for a in actions for v in a.get("environmentVariables",[]) if v["key"]=="NVIDIA_DRIVER_SOURCES"]
    if len(matches)!=1:
        r["runfile_witness"]={"actions":len(actions),"environment_entries":len(matches)};save();return
    expanded=json.loads(matches[0])
    rc,s,_=call(r,"root-output-base",["bazel","info","output_base"])
    base=Path(s.strip());verified={}
    for v,expected in allpins.items():
        anchor=expanded.get(v)
        if expected is None:
            verified[v]={"absent":v in expanded and anchor is None};continue
        canon=r["root_sources"]["nvidia_driver_"+v.replace(".","_")]
        expected_path="../"+canon+"/COPYING"
        repo=base/"external"/canon
        witness={"expanded":anchor==expected_path}
        for rel in ("COPYING","src/common/sdk/nvidia/inc/nvos.h"):
            file=repo/rel
            if file.is_file():
                data=file.read_bytes();witness[rel]=[sha(data),len(data)]
        verified[v]=witness
    r["runfile_witness"]=verified
    r["actual_paths_ok"]=set(verified)==set(allpins) and all(x["absent"] if allpins[v] is None else x["expanded"] and x.get("COPYING") and x.get("src/common/sdk/nvidia/inc/nvos.h") for v,x in verified.items());save()
def root_abi(r,arm):
    target="//pkg/sentry/devices/nvproxy:nvproxy_driver_abi_test";bep=TMP/"abi.bep"
    call(r,"abi-command",["timeout","-k","45s","35m","bazel","test","--config=x86_64","--jobs=4","--nocache_test_results","--test_timeout=600","--test_env=GO_TEST_WRAP_TESTV=1","--build_event_json_file="+str(bep),target])
    if bep.exists():
        for ln in bep.open():
            e=json.loads(ln)
            if e["id"].get("testResult",{}).get("label")!=target:continue
            t=e["testResult"];p=ROOT/"bazel-testlogs/pkg/sentry/devices/nvproxy/nvproxy_driver_abi_test/test.log"
            if p.is_file():
                b=p.read_bytes();s=b.decode(errors="replace")
                r["abi"]={"status":t["status"],"sha256":sha(b),"cached":bool(t.get("cachedLocally") or t.get("executionInfo",{}).get("cachedRemotely")),
                          "cases":{v:st for st,v in re.findall(r"--- (PASS|FAIL|SKIP): TestStructDefinitionParity/("+VERSION+")",s)},
                          "errors":[public(l) for l in s.splitlines() if re.search(r"\.go:\d+:|^panic:",l) and "no public source tag" not in l][:5]};save()
def result():
    a=report["arms"].get("parent",{});b=report["arms"].get("revision",{})
    def tidied(r):return r.get("tidy1",{}).get("exit")==0 and r.get("tidy2",{}).get("exit")==0 and not r["tidy1"]["driver_changes"] and not r["tidy2"]["changes"]
    abi=b.get("abi",{})
    report["verdict"]={"roots":all(r.get("root_ok") for r in [a,b]),"maps":set(a.get("client_sources",{}))==set(pins) and b.get("client_sources")=={} and b.get("dependency-map",{}).get("exit")==0,
        "consumer_built":bool(b.get("client_built")),"tidy":tidied(a) and tidied(b),
        "format":b.get("buildifier",{}).get("exit")==0 and b.get("gofmt",{}).get("exit")==0 and not b.get("gofmt_changed"),
        "generator":len(b.get("pin_runs",[]))==2 and all(x["exit"]==0 and x["before"]==x["after"] and len(x["tags"])==34 and not x["dirty"] for x in b["pin_runs"]),
        "licenses":b.get("verify",{}).get("exit")==0 and bool(b.get("verified_entries")) and len(b.get("catalog_sha_commit",{}))==25 and all(b["catalog_sha_commit"].values()) and bool(b.get("negative",{}).get("exit")) and b.get("actual_missing")==[NAME],
        "abi":abi.get("status")=="PASSED" and not abi.get("cached") and abi.get("cases")=={v:"PASS" if val else "SKIP" for v,val in allpins.items()}}
    report["verified"]=all(report["verdict"].values());save()
phase=sys.argv[1]
if phase=="done":result()
else:
    if phase in ("parent","revision"):
        commit=OLD if phase=="parent" else NEW
        git("checkout","--quiet","--detach",commit);assert git("rev-parse","HEAD")==commit
        r=report["arms"][phase]={};save()
        fns=[inventory,tidy,client]+([formatting,generate,license_audit,runfile_paths] if phase=="revision" else [])
    else:
        assert phase=="abi" and git("rev-parse","HEAD")==NEW;r=report["arms"]["revision"];fns=[root_abi]
    for fn in fns:
        try:fn(r,phase)
        except (OSError,ValueError,KeyError,AssertionError) as exc:r[fn.__name__+"_exception"]=public(str(exc)) or type(exc).__name__;save()
