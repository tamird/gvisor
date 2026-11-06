"""Compare configured work before conditionally running the released-source parser."""
import ast
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(os.environ["RUNNER_TEMP"])/"parser-cost"
REPORT = ROOT/"report.json"
SOURCES={"stock":"f51a205a8f4afbc5ab41aded070df74951b081ea","released":"5c019e9e6d5116841ee1dcbdee426437e9739027"}
ARCHES={"x86_64":("amd64",62,"78d4744ead1f5b19b04d0df558ab182a5300903900755e515b2ca4d10eb2044d"),
        "aarch64":("arm64",183,"b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02")}
OWNER="//tools/nvidia_driver_differ:driver_ast_parser_test"
BINARY=OWNER.removesuffix("_test")
BASE=["docker","exec","gnu",str(ROOT/"bazel"),"--nosystem_rc","--nohome_rc"]
REPO="--inject_repository=tcp_gnu="+str(ROOT/"platform")
COMMON=[REPO,"--extra_execution_platforms=@tcp_gnu//:gnu","--host_platform=@tcp_gnu//:gnu",
        "--lockfile_mode=off","--noshow_progress","--color=no","--curses=no","--use_target_platform_for_tests"]
def save(r):
    REPORT.write_text(json.dumps(r,indent=2)+"\n")

def select(source):
    subprocess.run(["git","checkout","--quiet","--detach",source],check=True)
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==source
    assert not subprocess.check_output(["git","diff","--name-only","HEAD"])

def run(r, entry, args, stem, seconds):
    iid=str(uuid.uuid4());entry["invocation"]=iid;save(r)
    out=ROOT/(stem+".out");err=ROOT/(stem+".err")
    # Global identifiers must be inserted immediately after the Bazel command.
    n=next(i for i,v in enumerate(args) if v in ("aquery","test"))
    with out.open("wb") as o,err.open("wb") as e:
        rc=subprocess.run(["timeout","-k","30",str(seconds),*args[:n+1],"--invocation_id="+iid,*args[n+1:]],stdout=o,stderr=e).returncode
    entry["exit"]=rc;entry["stderr_sha256"]=hashlib.sha256(err.read_bytes()).hexdigest()
    if rc:
        data=err.read_bytes().lower()
        entry["diagnostic_classes"]=[x.decode() for x in (b"error downloading",b"no such package",b"no matching toolchain",b"checksum",b"no space left",b"outofmemory",b"error loading package",b"undefined reference",b"no such file",b"test failed",b"resource_exhausted") if x in data]
    save(r);return out

def backends(kind):
    ob=subprocess.check_output([*BASE,"info","output_base"],text=True,stderr=subprocess.DEVNULL).strip()
    snippet="""import hashlib,json,sys
from pathlib import Path
base=Path(sys.argv[1])/'external'
found=[p/'llvm'/'targets.bzl' for p in base.iterdir() if p.name.endswith('+llvm-project') and p.name.startswith('llvm+')==(sys.argv[2]=='released') and (p/'llvm'/'targets.bzl').is_file()]
assert len(found)==1, [str(p) for p in found]
p=found[0];print(json.dumps([p.parents[1].name,p.read_text()]))"""
    repo,s=json.loads(subprocess.check_output(["docker","exec","gnu","python3","-c",snippet,ob,kind],text=True))
    lines=[line for line in s.splitlines() if re.match(r"^[a-zA-Z_]+\s*=",line)]
    assert len(lines)==1
    a=ast.literal_eval(lines[0].split("=",1)[1])
    assert len(a)==len(set(a)) and all(re.fullmatch(r"[A-Za-z0-9_]{1,40}",k) for k in a)
    return {"canonical_repository":repo,"targets_file_sha256":hashlib.sha256(s.encode()).hexdigest(),"backends":a}

def graphs(r):
    for kind,source in SOURCES.items():
        select(source)
        entry=r["sources"][kind]={"sha":source,"architectures":{}}
        for arch in ARCHES:
            cfg=entry["architectures"][arch]={}
            file=run(r,cfg,[*BASE,"aquery","--config="+arch,"-c","opt",*COMMON,"--noinclude_artifacts","--noinclude_commandline",
                "--output=jsonproto",'mnemonic("CppCompile",deps('+BINARY+'))'],kind+"-"+arch,1500)
            assert cfg["exit"]==0
            doc=json.loads(file.read_bytes());targets={str(t["id"]):t["label"] for t in doc["targets"]}
            actions=[a for a in doc["actions"] if a["mnemonic"]=="CppCompile"]; labels=[targets[str(a["targetId"])] for a in actions]
            repo=Counter(x.split("//",1)[0] or "_main" for x in labels)
            assert all(re.fullmatch(r"[A-Za-z0-9_@.+~-]{1,160}",k) for k in repo)
            llvm=sum(v for k,v in repo.items() if k.removeprefix("@@").removeprefix("@").endswith("llvm-project"))
            assert len(actions)>100 and llvm>100 and any(x.endswith(BINARY) for x in labels)
            cfg.update(cpp_compile=len(actions),llvm_cpp_compile=llvm,repositories=dict(repo.most_common(12)))
            if "backend_selection" not in entry:entry["backend_selection"]=backends(kind)
            save(r)
    for arch in ARCHES:
        a=r["sources"]["stock"]["architectures"][arch];b=r["sources"]["released"]["architectures"][arch]
        r.setdefault("delta",{})[arch]={"cpp_compile":b["cpp_compile"]-a["cpp_compile"],"llvm_cpp_compile":b["llvm_cpp_compile"]-a["llvm_cpp_compile"]}
    r["graph_status"]="COMPLETE";r["run_tests"]=all(v["cpp_compile"]<=0 for v in r["delta"].values());save(r)

def tests(r):
    assert r["graph_status"]=="COMPLETE"
    if not r["run_tests"]:return
    select(SOURCES["released"])
    for arch,(cpu,machine,image) in ARCHES.items():
        item=r.setdefault("tests",{}).setdefault(arch,{})
        under='/bin/sh -c \'printf "NATIVE_WORKER_ARCH=%s\\n" "$(uname -m)"; test "$(uname -m)" = '+arch+' || exit 125; exec "$@"\' parser'
        bep=ROOT/(arch+".bep")
        cmd=[*BASE,"--bazelrc="+str(ROOT/"private.rc"),"test","--config="+arch,"-c","opt",*COMMON,
             "--jobs=90","--remote_retries=1","--noremote_local_fallback","--strategy=TestRunner=remote",
             "--remote_default_exec_properties=Arch="+cpu,"--remote_default_exec_properties=OSFamily=linux",
             "--remote_default_exec_properties=workload-isolation-type=oci","--remote_default_exec_properties=container-image=docker://mirror.gcr.io/library/ubuntu@sha256:"+image,
             "--nocache_test_results","--test_sharding_strategy=disabled","--test_arg=-test.v","--test_env=GO_TEST_WRAP_TESTV=1","--run_under="+under,
             "--remote_download_outputs=toplevel","--remote_download_regex=.*/tools/nvidia_driver_differ/driver_ast_parser$","--build_event_json_file="+str(bep),OWNER]
        run(r,item,cmd,"test-"+arch,6400)
        if bep.exists():
            for line in bep.open():
                e=json.loads(line);k=e["id"]
                if k.get("testSummary",{}).get("label")==OWNER:item["bep_status"]=e["testSummary"].get("overallStatus")
                if k.get("testResult",{}).get("label")==OWNER:
                    t=e["testResult"];item["test_attempt"]={"status":t["status"],"cache":t.get("cachedLocally",False) or t.get("executionInfo",{}).get("cachedRemotely",False),"remote_strategy":t.get("executionInfo",{}).get("strategy")=="remote"}
        if item["exit"]==0:
            logdir="bazel-testlogs/tools/nvidia_driver_differ/driver_ast_parser_test"
            log=subprocess.check_output(["docker","exec","gnu","cat",logdir+"/test.log"])
            xml=subprocess.check_output(["docker","exec","gnu","cat",logdir+"/test.xml"])
            cases=[(x.get("name"),[c.tag for c in x]) for x in ET.fromstring(xml).iter("testcase")]
            item["native_machine"]=bool(("NATIVE_WORKER_ARCH="+arch).encode() in log)
            item["ran_parser"]=b"--- PASS: TestParser" in log and any("TestParser" in n and not set(tags)&{"skipped","failure","error"} for n,tags in cases)
            elf="""import hashlib,json,struct; f=open('bazel-bin/tools/nvidia_driver_differ/driver_ast_parser','rb'); a=f.read(); print(json.dumps([a[:6].hex(),struct.unpack_from('<H',a,18)[0],hashlib.sha256(a).hexdigest(),len(a)]))"""
            magic,num,sha,n=json.loads(subprocess.check_output(["docker","exec","gnu","python3","-c",elf]))
            item.update(elf_machine=num,elf_sha256=sha,elf_bytes=n)
            item["passed"]=(magic=="7f454c460201" and num==machine and item["native_machine"] and item["ran_parser"] and item.get("bep_status")=="PASSED" and item.get("test_attempt",{}).get("cache")==False and item["test_attempt"]["remote_strategy"])
        save(r)
    assert all(x.get("passed") for x in r["tests"].values())
    r["test_status"]="PASS"

mode=sys.argv[1]
r={"graph_status":"INCOMPLETE","sources":{},"parser":BINARY} if mode=="graphs" else json.loads(REPORT.read_bytes())
try:
    (graphs if mode=="graphs" else tests)(r)
except Exception as exc:
    r[mode+"_error"]={"type":type(exc).__name__,"line":traceback.extract_tb(exc.__traceback__)[-1].lineno}
    raise
finally:save(r)
