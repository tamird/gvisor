"""Build the same Sentry with two labels and verify the ELF notes from the links."""
from datetime import datetime, timezone
from hashlib import file_digest, sha256
import gzip
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
from zipfile import ZipFile

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "proof"
out.mkdir(exist_ok=True)
label = "//runsc/cmd/sentry:gvisor_sentry"
source = os.environ["SOURCE"]
coordinator = ["docker", "exec", "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
               "-e", "GIT_CONFIG_VALUE_0="+os.environ["GITHUB_WORKSPACE"], "overlay-guest"]

def now():
    return datetime.now(timezone.utc).isoformat()

def cmd(*args):
    return subprocess.check_output(args, text=True).strip()

def write(name, obj):
    (out/name).write_text(json.dumps(obj, indent=2)+"\n")

def notes(exe):
    dump = cmd("readelf", "--hex-dump=.note.go.buildid", "--wide", str(exe))
    words = []
    for line in dump.splitlines():
        if m := re.match(r"\s*0x[0-9a-f]+\s+((?:[0-9a-f]{8}(?:\s+|$)){1,4})", line):
            words.extend(m[1].split())
    data = bytes.fromhex("".join(words))
    namesz, size, typ = struct.unpack_from("<III", data)
    assert data[12:12+namesz].rstrip(b"\0")==b"Go" and typ==4
    pos=12+(namesz+3)//4*4
    go=data[pos:pos+size].decode()
    assert re.fullmatch(r"[0-9a-f]{20}(?:/[0-9a-f]{20}){3}",go),go
    gnu=re.findall(r"Build ID: ([0-9a-f]+)",cmd("readelf","--notes","--wide",str(exe)))
    assert len(gnu)==1 and len(gnu[0])>=32,gnu
    raw=go.replace("/","")
    assert raw[:len(gnu[0])]==gnu[0] and raw[-16:]==raw[:16] and set(raw)!={"0"}, (go,gnu)
    return go,gnu[0]

def run(arm,human):
    status=tmp/"sentry-label"
    status.write_text("#!/bin/sh\necho STABLE_VERSION "+human+"\n")
    status.chmod(0o755)
    profile=tmp/(arm+".profile.gz")
    bep=tmp/(arm+".bep");log=tmp/(arm+".log")
    args=["bazel","--bazelrc="+str(tmp/"overlay.bazelrc"),"build","--config=x86_64","-c","opt","--jobs=10",
          "--strategy=GoStaticAnalysis=linux-sandbox","--strategy=GoStandardLibraryAnalysis=linux-sandbox",
          "--strategy=GoStdlib=linux-sandbox","--strategy=GoStdlibList=linux-sandbox",
          "--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache,^GoLink$=+no-remote-cache",
          "--remote_download_outputs=toplevel","--stamp","--workspace_status_command="+str(status),
          "--experimental_profile_include_target_label","--profile="+str(profile),
          "--build_event_json_file="+str(bep),"--build_event_publish_all_actions", label]
    r={"source":source,"label":label,"stamp":human,"argv":args,"started":now()}
    with log.open("wb") as f:
        proc=subprocess.Popen([*coordinator,*args],stdout=f,stderr=subprocess.STDOUT)
        while proc.poll() is None:
            try: proc.wait(timeout=45)
            except subprocess.TimeoutExpired:
                print(arm,now(),log.stat().st_size,"log bytes",flush=True)
        r["exit"]=proc.returncode
    r["finished"]=now()
    lines=log.read_text(errors="replace").splitlines()
    r["diagnostics"]=[v for v in lines if ("Invocation ID:" in v or "ERROR:" in v or "INFO: Build" in v or "no such package" in v)][-35:]
    write(arm+".json",r)
    if r["exit"]:return r
    validate=[]
    with bep.open() as s:
        for line in s:
            v=json.loads(line); ac=v.get("action",{}); ev=v.get("id",{}).get("actionCompleted",{})
            if ac.get("type")=="GoLink" and (ev.get("label")==label or ev.get("primaryOutput","").endswith("/gvisor_sentry")):
                validate.append({k:ac[k] for k in ("success","exitCode","startTime","endTime","strategyDetails") if k in ac and k!="strategyDetails"}|{"output":ev.get("primaryOutput")})
    r["link_events"]=validate
    write(arm+".json",r)
    if not validate:
        r["link_evidence"]="BEP action absent; inspecting Bazel target output directly"
        path=cmd(*coordinator,"bash","-ec",'for f in bazel-bin/runsc/cmd/sentry/gvisor_sentry_/gvisor_sentry bazel-bin/runsc/cmd/sentry/gvisor_sentry; do if test -f "$f"; then readlink -f "$f"; exit; fi; done; exit 1')
    else:
        assert len(validate)==1 and validate[0].get("success") and validate[0]["output"],validate
        path=validate[0]["output"]
    exe=tmp/(arm+".sentry")
    if not path.startswith("/"):path=os.environ["GITHUB_WORKSPACE"]+"/"+path
    subprocess.run(["docker","cp","-L","overlay-guest:"+path,str(exe)],check=True)
    try:
        r["go_id"],r["gnu_id"]=notes(exe)
        with exe.open("rb") as f:r["sha256"]=file_digest(f,"sha256").hexdigest()
        r["bytes"]=exe.stat().st_size
        write(arm+".json",r)
        t=time.monotonic();r["version"]=cmd(str(exe),"--version").splitlines()
        r["version_process_seconds"]=time.monotonic()-t
        assert r["version"][0]=="runsc version "+human,r["version"]
    finally:
        exe.unlink(missing_ok=True)
    if profile.exists():
        with gzip.open(profile,"rt") as s: events=json.load(s).get("traceEvents",[])
        r["profile_link_spans"]=[{k:e.get(k) for k in ("name","cat","dur","args")} for e in events if e.get("name","").startswith("GoLink ") and "gvisor_sentry" in e.get("name","") and "gvisor_sentry" in str(e.get("args",""))][:8]
    return r

results={};problems=[]
assert cmd("git","rev-parse","HEAD")==source and not cmd("git","status","--porcelain","--untracked-files=no")
with ZipFile(tmp/"old.zip") as z: old=json.loads(z.read("verdict.json"))
assert old["executed"]==old["distinct_file_digests"]==12 and old["go_ids"]==["redacted"] and len(old["gnu_ids"])==1,old
write("stock-release-control.json",old)
for arm,human in (("a","sentry-id-a"),("b","sentry-id-b"),("a-again","sentry-id-a")):
    try:
        r=run(arm,human);results[arm]=r
        if r["exit"]:problems.append(arm+" Bazel exit "+str(r["exit"]))
    except Exception as err:
        results[arm]={"error":repr(err),"source":source,"stamp":human}
        problems.append(arm+": "+repr(err))
    write("arms.json",results)
if not problems:
    for key in ("sha256","gnu_id","go_id"):
        if results["a"][key]==results["b"][key] or results["a"][key]!=results["a-again"][key]:
            problems.append("A/B/A "+key+" mismatch")
if not results.get("a-again",{}).get("link_events"):
    problems.append("independent same-label link not witnessed in BEP")
assert not cmd("git","status","--porcelain","--untracked-files=no")
write("verdict.json",dict(source=source,expected=["a","b","a-again"],observed=list(results),
                          actual_labels=[label],prior_control="https://github.com/tamird/gvisor/actions/runs/41170208891/artifacts/13031275132",
                          verified=now(),errors=problems))
print(json.dumps(results,indent=2),problems)
sys.exit(bool(problems))
