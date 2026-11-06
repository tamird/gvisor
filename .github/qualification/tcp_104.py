"""Compile three pinned senders in the same workspace, then measure them serially."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlparse
import zipfile

root=Path.cwd()
tmp=Path(os.environ["RUNNER_TEMP"])/"queue104"
out=tmp/"original"
out.mkdir(exist_ok=True)
bazel=[tmp/"bazel","--nosystem_rc","--nohome_rc","--bazelrc=/dev/null","--host_jvm_args=-Xmx3g"]
flags=["--config=x86_64","--lockfile_mode=off","-c","opt","--jobs=4","--local_resources=memory=8192",
       "--strategy=GoStdlib=processwrapper-sandbox","--strategy=GoStdlibList=processwrapper-sandbox",
       "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
       "--spawn_strategy=linux-sandbox","--sandbox_default_allow_network=false",
       "--remote_cache=","--remote_executor=","--bes_backend="]
srcs={"stock":"32d64686c13190f83cb8411a23b036e0e102d4f3","fixed":"660437489757756b50254a3bc69b658b65d42a1b","calibration":"e2c58daaf8186926f092201063d8b3eb6442db18"}
export="a6f909f08a7250c080b5b70f3513e8b27b75f64a"
expected={"fixed":"e62be2c371d4d802eda696103d06ab69f6f56e82","calibration":"bfcc03e3c3af668490a36b0a94f9df5753d4b4d6"}
region="pkg/tcpip/transport/tcp/"
files=[region+x+".go" for x in ("rack","snd","state")]
builders=["pkg/"+name+"/BUILD" for name in ("goid","hosttid","sync")]
rendered=[f"pkg/{p}/{p}_asm_impl_{arch}.s" for p in ("goid","hosttid") for arch in ("amd64","arm64")]
rendered+=["pkg/sync/"+name for name in ("runtime_constants_go_impl.go","runtime_exectracer2_go_impl.go","runtime_spinning_asm_impl_amd64.s","runtime_spinning_asm_impl_arm64.s")]
target="//test/tcp_recovery_replay:reporter"
receipt={"export":export,"sources":srcs,"reporter":"94a018b41c986790ed76bd04b280ba85cd41f7bb","compiler_requested":"go1.27.3"}
def save():
    (out/"receipt.json").write_text(json.dumps(receipt,indent=2)+"\n")
def git(*args):
    return subprocess.check_output(["git",*args],text=True).strip()
def sha(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1<<20),b""):h.update(block)
    return h.hexdigest()
def command(name,cmd):
    log=tmp/(name+".log")
    with log.open("wb") as output:p=subprocess.run(cmd,stdout=output,stderr=subprocess.STDOUT)
    row=receipt[name]={"exit":p.returncode,"log_sha256":sha(log)}
    if p.returncode:
        lines=log.read_text(errors="replace").splitlines()
        row["errors"]=[l[-950:] for l in lines if any(k in l.lower() for k in ("error","could not","does not","failed","undefined","not found","cannot","unknown","missing","invalid"))][-34:]
        row["first_error_context"]=next(("\n".join(lines[i:i+18])[:6500] for i,l in enumerate(lines) if "ERROR:" in l),"")
        row["end"]="\n".join(lines[-14:])[-2500:]
    save()
    return p.returncode
def go_build(name,source):
    subprocess.run(["git","switch","--quiet","--detach",source],check=True)
    assert git("rev-parse","HEAD")==source
    assert set(git("diff","--name-only","HEAD","--","pkg").splitlines())==set(builders)
    assert all(git("hash-object",f)==receipt["shipped_generation"][f] for f in rendered)
    assert all(git("hash-object",f)==git("rev-parse",receipt["carrier"]+":"+f) for f in builders)
    code=command("build_"+name,[*bazel,"build",*flags,target])
    if code:return False
    cp=subprocess.run([*bazel,"cquery",*flags,"--output=files",target],capture_output=True,text=True)
    candidates=[]
    for line in cp.stdout.splitlines():
        if not line.startswith("bazel-out/"):continue
        path=root/line
        if path.is_file():
            with path.open("rb") as f:
                if f.read(4)==b"\x7fELF":candidates.append(path)
    digests={sha(p) for p in candidates}
    receipt["build_"+name].update({"query_exit":cp.returncode,"output_paths":[str(x.relative_to(root)) for x in candidates],"sha256":sorted(digests)})
    save()
    if cp.returncode or len(digests)!=1:return False
    binary=tmp/name
    shutil.copy2(candidates[0],binary)
    ver=subprocess.run(["go","version",binary],cwd=tmp,env={**os.environ,"GOTOOLCHAIN":"local","GOWORK":"off"},capture_output=True,text=True)
    version=ver.stdout.strip().split()[-1:] if ver.returncode==0 else []
    receipt["build_"+name]["go_version"]=version
    save()
    return version==[receipt["compiler_selected"]]
def collect(bep,label):
    safe={"samples.json",*(f"{v}-{seed:02}.txt" for v in srcs for seed in range(1,11))}
    paths=[]
    if bep.is_file():
        for line in bep.open():
            e=json.loads(line)
            if e.get("id",{}).get("testResult",{}).get("label")==label and "testResult" in e:
                tr=e["testResult"];receipt["testResult"]={"status":tr.get("status"),"cached":tr.get("cachedLocally",False),"strategy":tr.get("executionInfo",{}).get("strategy")}
                for rec in tr.get("testActionOutput",[]):
                    uri=urlparse(rec.get("uri",""))
                    if uri.scheme=="file":paths.append(Path(unquote(uri.path)))
    base=root/"bazel-testlogs/test/tcp_recovery_replay/measure/queue"
    paths.extend([base/"test.log",base/"test.outputs"/"outputs.zip",base/"test.outputs"/"samples.json"])
    for p in paths:
        if not p.is_file():continue
        if p.name=="test.log":
            raw=p.read_text(errors="replace")
            receipt["testlog_sha256"]=sha(p)
            receipt["logged_rows"]=[json.loads(l.removeprefix("QUEUE_SAMPLE ")) for l in raw.splitlines() if l.startswith("QUEUE_SAMPLE ")]
            if "testResult" not in receipt or receipt["testResult"]["status"]!="PASSED":receipt["test_end"]=raw[-3500:]
        elif p.suffix==".zip":
            with zipfile.ZipFile(p) as z:
                for k in z.namelist():
                    if k in safe and z.getinfo(k).file_size<500_000:(out/k).write_bytes(z.read(k))
        elif p.name in safe and p.stat().st_size<500_000:
            shutil.copyfile(p,out/p.name)
    unzipped=base/"test.outputs"
    if unzipped.is_dir():
        for name in safe:
            p=unzipped/name
            if p.is_file() and p.stat().st_size<500_000:shutil.copyfile(p,out/name)
    save()
def build():
    carrier=git("rev-parse","HEAD")
    assert git("rev-parse","HEAD^")==srcs["calibration"] and git("rev-parse","HEAD^^")==srcs["fixed"] and git("rev-parse","HEAD^^^")==srcs["stock"]
    subprocess.run(["git","fetch","--quiet","--depth=2","https://github.com/google/gvisor.git",export],check=True)
    assert git("rev-parse",export+"^2")==srcs["stock"]
    receipt["carrier"]=carrier
    receipt["shipped_generation"]={f:git("rev-parse",export+":"+f) for f in rendered}
    assert all(git("rev-parse",carrier+":"+f)==receipt["shipped_generation"][f] for f in rendered)
    receipt["export_blobs"]={f:git("rev-parse",export+":"+f) for f in files}
    assert all(git("rev-parse",srcs["stock"]+":"+f)==blob for f,blob in receipt["export_blobs"].items())
    receipt["patch_ids"]={}
    for name in expected:
        p=subprocess.check_output(["git","diff",srcs["stock"],srcs[name],"--","pkg"])
        receipt["patch_ids"][name]=subprocess.check_output(["git","patch-id","--stable"],input=p).split()[0].decode()
    assert receipt["patch_ids"]==expected
    save()
    git("switch","--quiet","--detach",srcs["stock"])
    git("restore","--source="+carrier,"--worktree","--","MODULE.bazel","go.mod","test/tcp_recovery_replay",*builders,*rendered)
    receipt["compiler_selected"]="go1.27.3"
    for name in srcs:
        if not go_build(name,srcs[name]):return 1
    m=root/"test/tcp_recovery_replay/measure"
    for name in srcs:
        shutil.copy2(tmp/name,m/name)
        assert [sha(m/name)]==receipt["build_"+name]["sha256"]
    # Verify the Python runner and its three data inputs before the measurement
    # step. Building the target does not start a fetch.
    return command("build_matrix",[*bazel,"build",*flags,"//test/tcp_recovery_replay/measure:queue"])

def measure():
    assert git("rev-parse","HEAD")==srcs["calibration"]
    for name in srcs:
        assert [sha(root/"test/tcp_recovery_replay/measure"/name)]==receipt["build_"+name]["sha256"]
    label="//test/tcp_recovery_replay/measure:queue";bep=tmp/"test.bep"
    code=command("matrix",[*bazel,"test",*flags,"--nocache_test_results","--flaky_test_attempts=1","--test_timeout=3250",
                          "--strategy=TestRunner=linux-sandbox","--test_output=errors","--build_event_json_file="+str(bep),label])
    collect(bep,label)
    rows=receipt.get("logged_rows",[])
    if code or len(rows)!=30 or not all(r["verified"] for r in rows) or receipt.get("testResult",{}).get("status")!="PASSED":
        return 1
    if any(r["status"]!="complete" for r in rows):
        receipt["benchstat"]="skipped: incomplete fetches";save()
        return 0
    labels={"stock":"stock","fixed":"PR16495","calibration":"PR16081"}
    inputs=[]
    for name in srcs:
        path=out/(name+".bench")
        path.write_text("".join(f"BenchmarkFetch 1 {round(r['seconds']*1e9)} ns/op\n" for r in rows if r["variant"]==name))
        inputs.append(labels[name]+"="+str(path))
    stderr=tmp/"benchstat.log"
    with (out/"benchstat.txt").open("wb") as of,stderr.open("wb") as ef:
        p=subprocess.run([*bazel,"run",*flags,"@org_golang_x_perf//cmd/benchstat","--",*inputs],stdout=of,stderr=ef)
    receipt["benchstat"]={"exit":p.returncode,"version":"406019bb8b68","sha256":sha(out/"benchstat.txt")}
    if p.returncode:receipt["benchstat"]["diagnostic"]=stderr.read_text(errors="replace")[-4300:]
    save()
    return p.returncode

mode=sys.argv[1]
if mode=="measure":
    receipt=json.loads((out/"receipt.json").read_text())
else:
    assert mode=="build"
save()
try:
    sys.exit(build() if mode=="build" else measure())
except Exception as e:
    receipt["exception"]=repr(e)[:1400];save();raise
