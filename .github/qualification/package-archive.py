"""Inspect the actual shipped package built from a source archive without Git."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile

def now():
    return datetime.now(timezone.utc).isoformat()

tmp = Path(os.environ["RUNNER_TEMP"])
source = os.environ["SOURCE"]
root = tmp / "package-source"
out = tmp / "proof"
root.mkdir()
out.mkdir()
assert subprocess.check_output(["git","rev-parse","HEAD^1"],text=True).strip()==source
with subprocess.Popen(["git","archive",source],stdout=subprocess.PIPE) as git:
    with tarfile.open(fileobj=git.stdout,mode="r|") as archive:
        archive.extractall(root,filter="tar")
    assert git.wait()==0
assert not (root/".git").exists() and not (root/"tools/workspace_status.sh").exists()
assert subprocess.run(["git","-C",str(root),"rev-parse","--is-inside-work-tree"],capture_output=True).returncode
rc=tmp/"overlay.bazelrc"
guest=["docker","exec","-w",str(root),"overlay-guest"]
common=[*guest,"bazel","--bazelrc="+str(rc),"build","-c","opt","--jobs=20",
        "--strategy=GoStaticAnalysis=linux-sandbox","--strategy=GoStandardLibraryAnalysis=linux-sandbox",
        "--strategy=GoStdlib=linux-sandbox","--strategy=GoStdlibList=linux-sandbox",
        "--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache",
        "--remote_download_outputs=toplevel"]
label="release-20990101.7"
lanes={"development-amd64":("amd64",[],"(devel)","0~devel"),
       "release-arm64":("arm64",["--config=release_version","--embed_label="+label],label,"20990101.7")}
expected=[f"{name}/{check}" for name in lanes for check in ("build","architecture","version","runsc")]
expected+=["status/legacy-version","status/reject-no-digits"]
target="//debian:debian"
results={}
reports={}
def check(key,ok,detail):
    results[key]=dict(passed=bool(ok),detail=detail)
    return bool(ok)
def run(name,flags,owner):
    bep=tmp/(name+".bep")
    with (tmp/(name+".log")).open("wb") as f:
        p=subprocess.run([*common,*flags,"--build_event_json_file="+str(bep),owner],stdout=f,stderr=subprocess.STDOUT)
    log=(tmp/(name+".log")).read_text(errors="replace")
    events=(json.loads(line) for line in bep.read_text().splitlines()) if bep.exists() else []
    completed={e["id"]["targetCompleted"]["label"]:e["completed"].get("success",False) for e in events if "targetCompleted" in e.get("id",{}) and "completed" in e}
    reports[name]=dict(exit=p.returncode,expected=[owner],completed=completed,invocations=sorted(set(re.findall(r"https://app.buildbuddy.io/invocation/[0-9a-f-]+",log))))
    if p.returncode:
        reports[name]["diagnostics"]=[s[:500] for s in log.splitlines() if ("ERROR:" in s or "package version" in s) and not re.search(r"(?i)(api[-_]key|secret|token|remote_header|bes_header)",s)][-6:]
    return p.returncode,completed,log
def archive(name,arch,flags,cli,debversion):
    config={"amd64":"x86_64","arm64":"aarch64"}[arch]
    code,completed,_=run(name,["--config="+config,*flags],target)
    if not check(name+"/build",code==0 and completed.get(target),reports[name]):return
    deb=tmp/(name+".deb")
    subprocess.run([*guest,"cp","bazel-bin/debian/runsc.deb",str(deb)],check=True)
    with deb.open("rb") as f: reports[name]["deb_sha256"]=hashlib.file_digest(f,"sha256").hexdigest()
    for field,want,key in (("Architecture",arch,"architecture"),("Version",debversion,"version")):
        got=subprocess.check_output(["dpkg-deb","-f",str(deb),field],text=True).strip()
        check(name+"/"+key,got==want,got)
    dest=tmp/("unpacked-"+name)
    subprocess.run(["dpkg-deb","-x",str(deb),str(dest)],check=True)
    cmd=[str(dest/"usr/bin/runsc"),"--version"]
    if arch=="arm64":cmd.insert(0,"qemu-aarch64")
    p=subprocess.run(cmd,capture_output=True,text=True)
    line=p.stdout.splitlines()[0] if p.stdout else ""
    check(name+"/runsc",p.returncode==0 and line=="runsc version "+cli,dict(exit=p.returncode,version=line))
    reports[name]["runner"]="qemu-aarch64" if arch=="arm64" else "native AMD64"
def legacy(key,label,positive):
    # Public callers may install their own Bazel workspace status command.
    status=tmp/("status-"+key)
    status.write_text("#!/bin/sh\nprintf 'STABLE_VERSION %s\\n' '"+label+"'\n")
    status.chmod(0o755)
    owner="//tools/bazeldefs:version"
    code,completed,log=run(key,["--config=x86_64","--config=release_version","--workspace_status_command="+str(status)],owner)
    if positive:
        got=subprocess.check_output([*guest,"cat","bazel-bin/tools/bazeldefs/version.txt"],text=True).strip() if code==0 else ""
        check("status/"+key,code==0 and completed.get(owner) and got=="20990101.8",got)
    else:
        check("status/"+key,code!=0 and completed.get(owner) is False and 'package version "untagged" must contain a digit' in log,reports[key])
for name,args in lanes.items():
    try:archive(name,*args)
    except Exception as e:reports[name+"/error"]=repr(e)
for key,val,ok in (("legacy-version","legacy-20990101.8",True),("reject-no-digits","untagged",False)):
    try:legacy(key,val,ok)
    except Exception as e:reports["status/"+key+"/error"]=repr(e)
finished=now()
failed=[k for k,v in results.items() if not v["passed"]]
missing=sorted(set(expected)-results.keys())
verdict=dict(source=source,expected=expected,executed=list(results),failed=failed,skipped=[],missing=missing,results=results,reports=reports,execution_finished=finished,verified_at=now(),passed=not failed and not missing and set(results)==set(expected))
(out/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
assert verdict["passed"],(failed,missing)
