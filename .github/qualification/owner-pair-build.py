"""Build two Sentries differing only in one startup event for each label policy."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from owner_inspect_elf import notes

tmp=Path(os.environ["RUNNER_TEMP"])
out=tmp/"label-proof"
out.mkdir(exist_ok=True)
group=sys.argv[1]
if group not in {"historical","historical-release","integrated","release"}: raise ValueError(group)
source=os.environ["BASELINE" if group.startswith("historical") else "SOURCE"]
name="release-20261026.0"
record=dict(group=group,source=source,result="FAIL",links={})
def persist():
    (out/("build-"+group+".json")).write_text(json.dumps(record,indent=2)+"\n")
def run(args,**kw):
    return subprocess.run([str(x) for x in args],check=True,**kw)
def text(*args):
    return subprocess.check_output([str(x) for x in args],text=True).strip()
docker=["docker","exec","-e","GIT_CONFIG_COUNT=1","-e","GIT_CONFIG_KEY_0=safe.directory",
        "-e","GIT_CONFIG_VALUE_0="+os.environ["GITHUB_WORKSPACE"],"overlay-guest"]
path=Path("runsc/cmd/sentry/sentrycmd/boot.go")
orig=b'timer.Reached("CPUID initialized")'
new=b'timer.Reached("CPU identification initialized")'
target="//runsc/cmd/sentry:gvisor_sentry"
probe="//test/cmd/test_app:test_app"
def hashfile(p):
    with Path(p).open("rb") as f: return hashlib.file_digest(f,"sha256").hexdigest()
def main():
    run(["git","checkout","--quiet","--detach",source])
    assert text("git","rev-parse","HEAD")==source
    clean=path.read_bytes(); assert clean.count(orig)==1
    # Execute the checkout's original status producer from outside any Git
    # repository; use those exact bytes for the two historical-display arms.
    stamp=tmp/"historical-source-export-status"
    original=tmp/"original-workspace-status.sh"
    original.write_bytes(subprocess.check_output(["git","show",os.environ["BASELINE"]+":tools/workspace_status.sh"]))
    original.chmod(0o755)
    empty=tmp/"outside-checkout"
    empty.mkdir(exist_ok=True)
    assert subprocess.run(["git","-C",str(empty),"rev-parse","--is-inside-work-tree"],capture_output=True).returncode!=0
    stamp.write_text("#!/bin/sh\ncd "+shlex.quote(str(empty))+" || exit 1\nexec "+shlex.quote(str(original))+"\n")
    stamp.chmod(0o755)
    emitted=text(*docker,stamp)
    assert emitted=="STABLE_VERSION 0.0.0",emitted
    release_stamp=tmp/"historical-qualified-release-status"
    release_stamp.write_text("#!/bin/sh\nprintf '%s\\n' 'STABLE_VERSION "+name+"'\n")
    release_stamp.chmod(0o755)
    record.update(export_status=emitted,status_script_sha256=hashfile(original),embed_label=name if group=="release" else "")
    for letter in (("A",) if group=="historical-release" else ("A","B")):
        arm=group+"-"+letter
        labels=[target,"//:release",probe] if letter=="A" else [target]
        path.write_bytes(clean if letter=="A" else clean.replace(orig,new))
        patch=subprocess.check_output(["git","diff","--",str(path)])
        assert bool(patch)==(letter=="B")
        (out/(arm+".patch")).write_bytes(patch)
        bep=tmp/(arm+".bep")
        flags=["--stamp","--workspace_status_command="+str(stamp)] if group!="release" else [
                "--config=release_version","--embed_label="+name,"--workspace_status_command="]
        if group=="historical-release":
            flags=["--stamp","--workspace_status_command="+str(release_stamp)]
        argv=[*docker,"bazel","--bazelrc="+str(tmp/"overlay.bazelrc"),"build","--config=x86_64",
             "--platforms=//tools/bazeldefs:linux_amd64","-c","opt","--jobs=10",
             "--strategy=GoStaticAnalysis=linux-sandbox","--strategy=GoStandardLibraryAnalysis=linux-sandbox",
             "--strategy=GoStdlib=linux-sandbox","--strategy=GoStdlibList=linux-sandbox",
             "--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache",
             "--remote_download_outputs=toplevel","--build_event_json_file="+str(bep),*flags,*labels]
        log=tmp/(arm+".log")
        with log.open("wb") as f: rc=subprocess.run(argv,stdout=f,stderr=subprocess.STDOUT).returncode
        body=log.read_text(errors="replace")
        r=dict(source=source,exit=rc,invocations=sorted(set(re.findall(r"https://app.buildbuddy.io/invocation/[0-9a-f-]+",body))),
               control_sha256=hashlib.sha256(patch).hexdigest(),flags=flags,expected=labels)
        record["links"][letter]=r;persist()
        if rc:
            (out/(arm+"-failure.txt")).write_text(re.sub(r"(?i)(x-buildbuddy-api-key[=: ]+)\S+",r"\1[redacted]",body[-6000:]))
            raise RuntimeError((arm,rc))
        passed=[]; refs=[]
        for line in bep.open():
            ev=json.loads(line); who=ev.get("id",{})
            if "targetCompleted" in who and ev.get("completed",{}).get("success"):
                passed.append(who["targetCompleted"]["label"]);refs.append(ev)
            if "namedSetOfFiles" in ev: refs.append(ev)
        (out/(arm+"-bep-outputs.json")).write_text(json.dumps(refs))
        assert set(passed)>=set(labels),(passed,labels)
        exe=tmp/(arm+".sentry")
        run([*docker,"cp","-L","bazel-bin/runsc/cmd/sentry/gvisor_sentry_/gvisor_sentry",exe])
        g,n=notes(exe); r.update(targets=sorted(set(passed)),go_id=g,gnu_id=n,sha256=hashfile(exe))
        expected_display="runsc version "+(name if group.endswith("release") else "0.0.0")
        r["sentry_version"]=text(exe,"--version").splitlines()[0]
        assert r["sentry_version"]==expected_display,r
        if letter=="A":
            dest=tmp/(group+"-release")
            run([*docker,"cp","-rL","bazel-bin/release",dest])
            run([*docker,"cp","-L","bazel-bin/test/cmd/test_app/test_app_/test_app",tmp/(group+"-guest")])
            assert hashfile(dest/"gvisor-bin/gvisor_sentry")==r["sha256"]
            r["cli_version"]=text(dest/"runsc","--version").splitlines()[0]
            assert r["cli_version"]==expected_display,r
            r["cli_sha256"]=hashfile(dest/"runsc")
        persist();print(arm,r["sentry_version"],r["go_id"],flush=True)
    assert len({v["go_id"] for v in record["links"].values()})==len(record["links"])
    assert len({v["sha256"] for v in record["links"].values()})==len(record["links"])
try:
    main();record["result"]="PASS"
except BaseException as e: record["error"]=repr(e);raise
finally:
    if text("git","rev-parse","HEAD")==source:
        run(["git","restore","--",str(path)])
    record["recorded"]=datetime.now(timezone.utc).isoformat();persist()
