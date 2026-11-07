"""Verify the original statefile tests and both non-root native workers."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

D = Path(os.environ["RUNNER_TEMP"]) / "permission-owner"
BASE = "d78fe07a0a829205c5e26b864c648efdcb4d098d"
SOURCE = "9a96308f8036d27da5be240b4a3863dc5a067210"
LABEL = "//runsc/cmd:statefile_output_permissions_test"
CASES = {"readonly_existing_file_rejected_when_unprivileged","dirs_without_read_perm_succeed_when_unprivileged"}
PROFILES = {"amd64":"source-presubmit", "arm64":"source-presubmit-arm64"}


def store(name, data):
    (D/"results"/(name+".json")).write_text(json.dumps(data,indent=2)+"\n")


def command(key, arch, args, remote=False):
    bep=D/(key+".bep")
    flags=["--bazelrc="+str(D/"remote.rc")] if remote else []
    with (D/(key+".log")).open("wb") as out:
        rc=subprocess.run([str(D/"bazel"),"--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx8g",*flags,
            "test","--config="+PROFILES[arch],*args,"--color=no","--curses=no","--noshow_progress",
            "--build_event_json_file="+str(bep)],stdout=out,stderr=subprocess.STDOUT).returncode
    return rc,(json.loads(line) for line in bep.open())


def actual(arch):
    observer = 'printf "PERMISSION_WORKER %s %s %s %s\\n" "$HOSTTYPE" "$UID" "$EUID" "$([ -w "$TEST_TMPDIR" ] && echo writable || echo inaccessible)"; exec "$@"'
    rc,events=command("actual-"+arch,arch,["--jobs=30","--noremote_local_fallback","--nocache_test_results",
        "--flaky_test_attempts=1","--modify_execution_info=^TestRunner$=+no-remote-cache",
        "--test_arg=-test.v","--test_env=GO_TEST_WRAP_TESTV=1","--remote_download_outputs=minimal",
        "--run_under=bash -c "+shlex.quote(observer)+" observe",
        "--remote_download_regex=.*testlogs/runsc/cmd/statefile_output_permissions_test/test[.](log|xml)",
        "--target_pattern_file="+str(D/"owner.targets")],True)
    rows=[];inv=None
    for event in events:
        if "started" in event:inv=str(uuid.UUID(event["started"]["uuid"]))
        if "testResult" not in event:continue
        assert event["id"]["testResult"]["label"]==LABEL
        r=event["testResult"];info=r.get("executionInfo",{})
        row=dict(status=r["status"],strategy=info.get("strategy"),cached=bool(r.get("cachedLocally") or info.get("cachedRemotely")))
        for f in r["testActionOutput"]:
            name=f["name"]
            if name not in ("test.xml","test.log"):continue
            file=Path("bazel-testlogs/runsc/cmd/statefile_output_permissions_test")/name
            if not file.is_file():continue
            data=file.read_bytes();h=hashlib.sha256(data).hexdigest()
            digest=re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/\d+(?:$|[?])",f.get("uri",""))
            if h!=(digest[1] if digest else f.get("digest")):continue
            row[name]=h
            if name=="test.log":
                token=re.findall(rb"(?m)^PERMISSION_WORKER (x86_64|aarch64) (\d+) (\d+) (writable|inaccessible)\s*$",data)
                if len(token)==1:
                    row["observed_cpu"]={b"x86_64":"amd64",b"aarch64":"arm64"}[token[0][0]]
                    row["uid"]=int(token[0][1])
                    row["euid"]=int(token[0][2])
                    row["temp_writable"]=token[0][3]==b"writable"
                row["go_pass"]=sorted(c for c in CASES if ("--- PASS: TestStatefileOutputPermissions/"+c+" (").encode() in data)
            else:
                states={}
                for tc in ET.fromstring(data).iter("testcase"):
                    for c in CASES:
                        if tc.get("name","").endswith("TestStatefileOutputPermissions/"+c):
                            assert c not in states
                            states[c]="PASS" if not any(tc.find(x) is not None for x in ("failure","error","skipped")) else "FAIL_OR_SKIP"
                row["cases"]=states
        rows.append(row)
    ok=rc==0 and len(rows)==1 and all(r["strategy"]=="remote" and r["status"]=="PASSED" and not r["cached"] and
        r.get("observed_cpu")==arch and r.get("uid")==65534 and r.get("euid")==65534 and r.get("temp_writable") and r.get("go_pass")==sorted(CASES) and r.get("cases")==dict.fromkeys(CASES,"PASS") for r in rows)
    store("actual-"+arch,dict(source=SOURCE,base=BASE,invocation=inv,exit=rc,rows=rows,verified=ok))
    return ok


def worker(arch):
    sys.path.insert(0,str(D))
    import exec_cas as cas
    from source_presubmit_workers import platform_fields
    cas.INV=str(uuid.UUID(json.loads((D/"results"/("actual-"+arch+".json")).read_text())["invocation"]))
    rows=[]
    for e in cas.executions(LABEL,"TestRunner"):
        rep=e.get("executeResponse",{});out=rep.get("result")
        if out is None:continue
        action=dict(cas.fields(cas.blob(e["actionDigest"],128*1024)));cmd=list(cas.fields(cas.blob(cas.digest_msg(action[1]),4*1024*1024)))
        args=[v.decode() for t,v in cmd if t==1]
        if not (any(a.endswith("/test-setup.sh") for a in args) or any(f.get("path","").endswith("/test.cache_status") for f in out.get("outputFiles",[]))):continue
        p=platform_fields(action[10]) if 10 in action else next((platform_fields(v) for t,v in cmd if t==5),{})
        rows.append([p.get("cpu"),p.get("class"),p.get("linux"),p.get("ubuntu"),p.get("requested_nobody"),p.get("nonroot_workspace"),any("qemu-" in a for a in args),out.get("exitCode",0),rep.get("status",{}).get("code",0),bool(rep.get("cachedResult"))])
    ok=rows==[[arch,"oci",True,True,True,True,False,0,0,False]]
    store("worker-"+arch,dict(verified=ok,rows=rows));return ok


if __name__=="__main__":
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
    flags=[]
    for arch in PROFILES:
        try:flags.append({"actual":actual,"workers":worker}[sys.argv[1]](arch))
        except (OSError,KeyError,ValueError,AssertionError,ET.ParseError,subprocess.SubprocessError) as err:
            store(sys.argv[1]+"-error-"+arch,{"error":type(err).__name__});flags.append(False)
    if not all(flags):raise SystemExit("incomplete results")
