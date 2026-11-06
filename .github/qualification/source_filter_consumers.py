"""Report exactly the two generated filters, selected test roots and any first failing actions."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

SOURCE="53c458937842fb431ef351c39781850f5a817234"
TMP=Path(os.environ["RUNNER_TEMP"])
assert not (TMP/"source-presubmit.bazelrc").exists()
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
GENERATORS={"//runsc/boot/filter:filter_precompiled","//runsc/gofer/net/filter:filter_precompiled"}
TESTS={"//runsc:version_test","//runsc/gofer/net:netgofer_test","//test/syscalls:uname_test_native"}
ROOTS=GENERATORS|TESTS
WORDS=["PrecompileSeccomp","command not found","No such file or directory","exec format error","Permission denied","Operation not permitted",
       "failed to start","executable file","no such target","analysis of target","does not provide","no matching toolchain","incompatible",
       "aborted","error:","undefined reference","FileNotFoundError","cannot find","cgroup","Invalid argument","QEMU","qemu-x86_64",
       "qemu-aarch64","illegal instruction","fatal error","SIGILL","Segmentation fault","SIGSEGV","exited with status","killed",
       "runtime:","failed to load shared libraries","undefined symbol","out of memory","outofmemoryerror","timeout","timed out",
       "not found in PATH","requires","PANIC","expected","could not","unable to","panic:","CGROUPV2","no space left on device",
       "Bad system call","SIGSYS","Operation not supported","test setup script","qemu-user","wrong ELF","resource exhausted"]
MNEMONICS={"PrecompileSeccomp","GoCompilePkg","GoLink","CppCompile","CppLink","TestRunner","GoToolchainBinaryBuild","GoStdlib","Genrule","GoNogo"}
def sha(data):return hashlib.sha256(data).hexdigest()
def public(label):return label if label in ROOTS else hashlib.sha256(label.encode()).hexdigest()
def signals(text):return [s for s in WORDS if s.lower() in text.lower()]
def ref(file):
    m=re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$",file.get("uri",""))
    return [m[1],int(m[2])] if m else None
report={"source":SOURCE,"original":"bc29a60b-916d-49ee-b194-acb6c21cf98f","targets":{},"generators":{},"tests":{},"failed_actions":[],
        "coordinator":json.loads((TMP/"source-presubmit-memory.json").read_text()), "verified":False}
def save():(TMP/"source-filter-consumers.json").write_text(json.dumps(report,separators=(",",":")))
save()
bep=TMP/"source-presubmit.bep"
if bep.is_file():
    for line in bep.open():
        e=json.loads(line); ident=e["id"]; owner=ident.get("targetCompleted",{}).get("label"); a=e.get("action")
        if "started" in e and re.fullmatch("[a-f0-9-]{36}",e["started"].get("uuid","")):report["invocation"]=e["started"]["uuid"]
        if "finished" in e:report["exit"]=e["finished"].get("exitCode",{}).get("code",0)
        if owner in ROOTS and not ident["targetCompleted"].get("aspect"):
            report["targets"][owner]="built" if e.get("completed",{}).get("success") else "failed" if "completed" in e else "aborted"
        if a:
            label=ident.get("actionCompleted",{}).get("label","")
            if label in GENERATORS and a.get("type")=="PrecompileSeccomp":
                report["generators"].setdefault(label,{})["action_success"]=bool(a.get("success"))
            if not a.get("success") and len(report["failed_actions"])<10:
                report["failed_actions"].append({"owner":public(label),"type":a["type"] if a.get("type") in MNEMONICS else "other",
                    "exit":int(a.get("exitCode",0)),"signals":signals(str(a.get("failureDetail",{}))),"stderr":ref(a.get("stderr",{}))})
        t=e.get("testResult"); owner=ident.get("testResult",{}).get("label")
        if not t or owner not in TESTS:continue
        info=t.get("executionInfo",{}); r={"status":t["status"],"remote":info.get("strategy")=="remote",
            "cached":bool(info.get("cachedRemotely") or t.get("cachedLocally"))}
        report["tests"].setdefault(owner,[]).append(r)
        for f in t.get("testActionOutput",[]):
            name=f.get("name");uri=urlparse(f.get("uri",""))
            if name not in {"test.xml","test.log"}:continue
            path=Path(unquote(uri.path)) if uri.scheme=="file" else Path("bazel-testlogs")/owner[2:].replace(":","/")/name
            if not path.is_file():continue
            data=path.read_bytes(); blob=ref(f); digest=sha(data)
            if blob and blob != [digest,len(data)]:continue
            r[name]=[digest,len(data)]
            if name=="test.xml":
                try:
                    cases=ET.fromstring(data).findall(".//testcase")
                    r["case_count"]=len(cases)
                    r["case_failures"]=sum(v.find("failure") is not None or v.find("error") is not None for v in cases)
                    r["case_skips"]=sum(v.find("skipped") is not None for v in cases)
                except ET.ParseError:r["xml_invalid"]=True
            else:r["log_signals"]=signals(data.decode(errors="replace"))
for owner in GENERATORS:
    path=Path("bazel-bin")/owner.split(":")[0][2:]/"filter_precompiled.go"
    if path.is_file():
        data=path.read_bytes()
        report["generators"].setdefault(owner,{}).update(sha256=sha(data),bytes=len(data),go_package=bool(re.search(rb"(?m)^package filter$",data)))
save()
log=TMP/"source-presubmit.log"; diag=[]
tracked=set(subprocess.check_output(["git","ls-files","-z"]).decode().split("\0"))
if log.is_file():
    for line in log.open(errors="replace"):
        if not re.search(r"(?i)^\s*ERROR:|error:|target.*failed|No such file",line):continue
        primary=[l for l in ROOTS if l in line]
        locations=[]
        for f,n in re.findall(r"([-A-Za-z0-9_./+]+):([0-9]{1,6})",line):
            parts=f.strip("/").split("/")
            found=next(("/".join(parts[i:]) for i in range(len(parts)) if "/".join(parts[i:]) in tracked),None)
            if found:locations.append([found,int(n)])
        witness={"roots":primary,"locations":locations[:3],"signals":signals(line)}
        if witness not in diag and len(diag)<24:diag.append(witness)
report["console"]=diag
report["verified"]=(report.get("exit")==0 and set(report["targets"])==ROOTS and all(v=="built" for v in report["targets"].values())
    and set(report["generators"])==GENERATORS and all(v.get("go_package") for v in report["generators"].values())
    and set(report["tests"])==TESTS and all(len(rs)==1 and rs[0]["remote"] and not rs[0]["cached"] and rs[0]["status"]=="PASSED" for rs in report["tests"].values()))
save()
