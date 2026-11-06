"""Run the current integration's executable SDK owners on the ARM64 host."""
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse

work = Path(os.environ["RUNNER_TEMP"]) / "native-sdk"
expected = "38745c527784376e0b5d915fe5ebd85e1d1385fd"
go = ["//examples/sandboxexec/go/docprocessor:docprocessor_test","//sandboxexec/sandbox:sandbox_test"]
python_suite = "//sandboxexec/sandbox/python:sandbox_py_test"
receipt = {"source":expected,"machine":platform.machine(),"kernel":platform.release(),"uid":os.geteuid(),"runs":{}}

def save():
    (work / "result.json").write_text(json.dumps(receipt,indent=2)+"\n")

def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f,"sha256").hexdigest() if hasattr(hashlib,"file_digest") else hashlib.sha256(f.read()).hexdigest()

def stdlib_probe():
    console = work/"stdlib.log"; events=work/"stdlib.events"; exec_log=work/"stdlib.exec"
    inv=str(uuid.uuid4());row=receipt["stdlib"]={"invocation":inv};save()
    with console.open("wb") as out:
        p=subprocess.run(["timeout","-k","15","1050",str(work/"bazel"),"--nosystem_rc","--nohome_rc","build",
            "--invocation_id="+inv,"--config=aarch64","--jobs=4","--spawn_strategy=sandboxed,local","--lockfile_mode=off",
            "--execution_log_json_file="+str(exec_log),"--build_event_publish_all_actions","--build_event_json_file="+str(events),"--noshow_progress","--color=no","--curses=no",
            "//tools/nogo:stdlib"],stdout=out,stderr=subprocess.STDOUT)
    row["exit"]=p.returncode;row["log_sha256"]=sha(console)
    lines=console.read_text(errors="replace").splitlines()
    row["diagnostic"]=[]
    window=0
    for line in lines:
        if line.startswith("ERROR:") or "error walking" in line or "error resolving" in line or "panic:" in line:window=14
        if window and len(row["diagnostic"])<45:
            row["diagnostic"].append(line[:900]);window-=1
    if p.returncode and not row["diagnostic"]:row["diagnostic"]=lines[-15:]
    row["actions"]=[]
    if events.is_file():
        for line in events.open():
            ev=json.loads(line);act=ev.get("action",{})
            if act.get("type")!="GoStandardLibraryAnalysis":continue
            output=act.get("stderr",{});uri=output.get("uri","")
            action={"exit":act.get("exitCode",0),"success":act.get("success",False),"key":ev.get("id",{}).get("actionCompleted",{}).get("label")}
            if uri.startswith("file://"):
                f=Path(unquote(urlparse(uri).path))
                if f.is_file():
                    action["stderr_sha256"]=sha(f);action["stderr"]=f.read_text(errors="replace").splitlines()[:12]
                    action["stderr"]=[l[:900] for l in action["stderr"]]
            if len(row["actions"])<8:row["actions"].append(action)
    if exec_log.is_file():
        query='if type=="array" then .[] else . end | select(.mnemonic=="GoStdlib" or .mnemonic=="GoStandardLibraryAnalysis") | {mnemonic,owner:.targetLabel,runner,exit:(.exitCode//0),cached:(.cacheHit//false),outputs:(.listedOutputs//[])}'
        projected=subprocess.run(["jq","-c",query,exec_log],capture_output=True,text=True)
        row["projection_exit"]=projected.returncode
        row["producers"]=[]
        for line in projected.stdout.splitlines()[:12]:
            item=json.loads(line);roots=item.pop("outputs",[])
            for raw in roots[:10]:
                pp=Path(raw)
                if not pp.parts or pp.parts[0]!="bazel-out" or ".." in pp.parts or not pp.is_dir():continue
                archives=list(pp.rglob("*.a"))
                if archives:
                    item["archive_count"]=len(archives)
                    item["fmt_archive"]=any(v.name=="fmt.a" for v in archives)
                    item["excluded_benchmark_archives"]=sum(str(v).endswith("/log/slog/internal/benchmarks.a") for v in archives)
            row["producers"].append(item)
    binpath=Path("bazel-bin/tools/nogo/stdlib.facts")
    if binpath.is_file():row["facts"]={"bytes":binpath.stat().st_size,"sha256":sha(binpath)}
    save()
    return p.returncode==0 and binpath.is_file() and binpath.stat().st_size>0

def summarize(name, labels, extra, limit):
    bep = work / (name+".events")
    console = work / (name+".log")
    entry = receipt["runs"][name] = {"labels":labels,"attempts":{},"invocation":str(uuid.uuid4()),"initial_free_bytes":shutil.disk_usage(work).free}
    save()
    command = [str(work/"bazel"),"--nosystem_rc","--nohome_rc","test","--invocation_id="+entry["invocation"],
               "--config=aarch64","--jobs=4","--local_test_jobs=1","--spawn_strategy=sandboxed,local","--strategy=TestRunner=local",
               "--nocache_test_results","--flaky_test_attempts=1","--runs_per_test=1","--keep_going","--test_output=errors",
               "--test_env=GO_TEST_WRAP_TESTV=1","--test_env=GO_TEST_RUN_FROM_BAZEL=1","--test_env=DOCKER_HOST=unix:///nonexistent-sdk-docker.sock",
               "--lockfile_mode=off","--noshow_progress","--color=no","--curses=no","--build_event_json_file="+str(bep),*extra,*labels]
    with console.open("wb") as out:
        process = subprocess.run(["timeout","-k","20",str(limit),*command],stdout=out,stderr=subprocess.STDOUT)
    entry.update(exit=process.returncode,log_sha256=sha(console),final_free_bytes=shutil.disk_usage(work).free)
    limit_errors = []
    for l in console.open(errors="replace"):
        if l.startswith("ERROR:") or "No space left on device" in l or "OutOfMemoryError" in l:
            if len(limit_errors)<6:limit_errors.append(l.strip()[:750])
    entry["first_errors"]=limit_errors
    raw_lines=console.read_text(errors="replace").splitlines()
    first=next((i for i,l in enumerate(raw_lines) if l.startswith("ERROR:")),None)
    if first is not None:entry["first_failure_context"]=[line[:800] for line in raw_lines[first:first+16]]
    if bep.exists():
        for line in bep.open():
            evt=json.loads(line)
            label=evt.get("id",{}).get("testResult",{}).get("label")
            if label not in labels or "testResult" not in evt:continue
            result=evt["testResult"];rec=entry["attempts"].setdefault(label,{})
            rec.update(status=result.get("status"),cached=result.get("cachedLocally",False),strategy=result.get("executionInfo",{}).get("strategy"),xml=[],log_lines=[])
            for output in result.get("testActionOutput",[]):
                key=output.get("name","")
                if key not in ("test.xml","test.log") or not output.get("uri","").startswith("file://"):continue
                path=Path(unquote(urlparse(output["uri"]).path))
                if not path.is_file():continue
                rec[key+"_sha256"]=sha(path)
                if key=="test.xml":
                    for tc in ET.parse(path).iter("testcase"):
                        state=next((s for s in ("failure","error","skipped") if tc.find(s) is not None),"passed")
                        rec["xml"].append({"name":tc.get("name","")[:180],"status":state})
                else:
                    lines=path.read_text(errors="replace").splitlines()
                    rec["log_lines"]=[l.strip()[:340] for l in lines if l.lstrip().startswith(("--- FAIL:","--- SKIP:","FAIL:","ERROR:","Ran ","OK","FAILED ("))][:30]
                    if label==python_suite:
                        pattern=r"^(test_\w+) \(.+?\) \.\.\. (ok|skipped.*|FAIL|ERROR)$"
                        rec["python"]={m[1]:m[2] for l in lines if (m:=re.match(pattern,l))}
                    if result.get("status")!="PASSED":
                        rec["diagnostic"]=[l.strip()[:580] for l in lines if any(s in l.lower() for s in ("eperm","failed","error:","permission","operation not permitted","no such file","panic:","expected"))][:12]
            save()
    save()

assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==expected
assert receipt["machine"]=="aarch64" and receipt["uid"]!=0
receipt["unprivileged_userns"]=subprocess.run(["unshare","--user","--map-root-user","true"]).returncode
receipt["cgroup_v2"]=Path("/sys/fs/cgroup/cgroup.controllers").is_file()
receipt["initial_free_bytes"]=shutil.disk_usage(work).free
save()
if not stdlib_probe():
    raise SystemExit("Native standard-library analyzer failed before SDK tests")
summarize("go",go,[],2100)
summarize("python",[python_suite],["--test_arg=-v"],1050)
binary=Path("bazel-bin/release/runsc")
if binary.is_file():
    with binary.open("rb") as f:header=f.read(20)
    receipt["runsc"]={"sha256":sha(binary),"bytes":binary.stat().st_size,"elf_machine":int.from_bytes(header[18:20],"little"),"elf":header[:6]==b"\x7fELF\x02\x01"}
test_source=Path("sandboxexec/sandbox/python/tests/sandbox_test.py")
receipt["expected_python"]=sorted(n.name for n in ast.walk(ast.parse(test_source.read_text())) if isinstance(n,ast.FunctionDef) and n.name.startswith("test_"))
save()
assert not subprocess.check_output(["git","diff","--name-only","HEAD"])
totals={label:r for row in receipt["runs"].values() for label,r in row["attempts"].items()}
assert set(totals)==set([*go,python_suite]) and all(r["exit"]==0 for r in receipt["runs"].values())
assert all(r["status"]=="PASSED" and not r["cached"] for r in totals.values())
assert receipt["runsc"]["elf"] and receipt["runsc"]["elf_machine"]==183
for label in go:
    assert totals[label]["xml"] and all(c["status"]=="passed" for c in totals[label]["xml"])
assert set(totals[python_suite]["python"])==set(receipt["expected_python"]) and all(val=="ok" for val in totals[python_suite]["python"].values())
