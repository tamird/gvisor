import subprocess
import re
import struct
def cmd(*args):
    return subprocess.check_output(args, text=True).strip()
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
"""Build the launcher and two pairs of unnamed sentries from recorded inputs."""
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import re
import shutil
import subprocess

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "proof"
out.mkdir(exist_ok=True)
install = tmp / "identity-release"
outcomes = {}
target = "//runsc/cmd/sentry:gvisor_sentry"
exec_prefix = ["docker", "exec", "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
               "-e", "GIT_CONFIG_VALUE_0=" + os.environ["GITHUB_WORKSPACE"], "overlay-guest"]
path = Path("runsc/cmd/sentry/sentrycmd/boot.go")
run = subprocess.run
for group in ("original", "prototype"):
    source = os.environ[group.upper()]
    run(["git", "checkout", "--quiet", "--detach", source], check=True)
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == source
    clean = path.read_bytes()
    old = b'timer.Reached("CPUID initialized")'
    assert clean.count(old) == 1
    for letter in ("A","B"):
        name = group + "-" + letter
        case = out / name
        case.mkdir()
        if letter == "B":
            path.write_bytes(clean.replace(old,b'timer.Reached("CPU identification initialized")'))
        diff = subprocess.check_output(["git","diff","--",str(path)])
        (case/"control.patch").write_bytes(diff)
        assert bool(diff) == (letter == "B")
        labels = [target] if name != "original-A" else ["//:release", "//test/cmd/test_app", target]
        bep = tmp/(name+".bep")
        argv = ["bazel","--bazelrc="+str(tmp/"overlay.bazelrc"),"build","--config=x86_64","-c","opt","--jobs=12",
                "--strategy=GoStaticAnalysis=linux-sandbox","--strategy=GoStandardLibraryAnalysis=linux-sandbox",
                "--strategy=GoStdlib=linux-sandbox","--strategy=GoStdlibList=linux-sandbox",
                "--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache",
                "--remote_download_outputs=toplevel","--stamp","--workspace_status_command=",
                "--build_event_json_file="+str(bep),*labels]
        logfile = tmp/(name+".log")
        with logfile.open("wb") as f:
            result = run([*exec_prefix,*argv],stdout=f,stderr=subprocess.STDOUT)
        text = logfile.read_text(errors="replace")
        invocations = sorted(set(re.findall(r"https://app.buildbuddy.io/invocation/[0-9a-f-]+", text)))
        (case/"bazel.json").write_text(json.dumps(dict(source=source,targets=labels,exit=result.returncode,invocations=invocations,
            control_sha256=hashlib.sha256(diff).hexdigest(),finished=datetime.now(timezone.utc).isoformat()),indent=2)+"\n")
        if result.returncode:
            safe = re.sub(r"(?i)(x-buildbuddy-api-key[=: ]+)\S+",r"\1[redacted]",logfile.read_text(errors="replace")[-5000:])
            (case/"failure.txt").write_text(safe)
            raise RuntimeError((name,result.returncode,safe))
        events = [json.loads(line) for line in bep.read_text().splitlines()]
        # Preserve CAS addresses on the worker, so a runtime-only recovery
        # can fetch these same files independently of Bazel's local outputs.
        target_files = [ev for ev in events if "namedSetOfFiles" in ev or "targetCompleted" in ev.get("id",{})]
        (case/"bep-targets.json").write_text(json.dumps(target_files)+"\n")
        passed = {e["id"]["targetCompleted"]["label"] for e in events
                  if "targetCompleted" in e.get("id",{}) and e.get("completed",{}).get("success")}
        expected = {target} if name!="original-A" else {target,"//:release","//test/cmd/test_app:test_app"}
        assert passed >= expected, (passed,expected)
        # Bazel symlinks refer to locations private to the Linux coordinator.
        exe = tmp/(name+".sentry")
        run([*exec_prefix,"cp","-L","bazel-bin/runsc/cmd/sentry/gvisor_sentry_/gvisor_sentry",str(exe)],check=True)
        if name == "original-A":
            run([*exec_prefix,"cp","-rL","bazel-bin/release",str(install)],check=True)
            run([*exec_prefix,"cp","-L","bazel-bin/test/cmd/test_app/test_app_/test_app",str(tmp/"identity-guest")],check=True)
        version = subprocess.check_output([str(exe),"--version"],text=True).splitlines()[0].strip()
        assert version in {"runsc version","runsc version VERSION_MISSING"},version
        with exe.open("rb") as f:
            digest = hashlib.file_digest(f,"sha256").hexdigest()
        go_id,gnu_id = notes(exe)
        outcomes[name] = dict(source=source,control_patch=diff.decode(),executed_targets=sorted(passed),
                              expected_targets=sorted(expected),sha256=digest,go_id=go_id,gnu_id=gnu_id,
                              version=version)
        (out/"builds.json").write_text(json.dumps(outcomes,indent=2)+"\n")
        print(name,version,go_id,flush=True)
        path.write_bytes(clean)
assert len({x["go_id"] for x in outcomes.values()}) == len(outcomes) == 4
