"""Compare an observed proc boot ID with old and new mounts after real restore."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

def now():
    return datetime.datetime.now(datetime.UTC).isoformat()

tmp = Path(os.environ["RUNNER_TEMP"])
install = tmp / "install-fixed"
bundle = tmp / "checkpoint-bundle"
proof = tmp / "boot-identity-results"
fs = bundle / "rootfs"
image = bundle / "image"
case = "restore"
expected = ["before save", "restored existing mounts", "new mount after restore"]
out = dict(source=os.environ["SOURCE"], product=os.environ["FIX_PRODUCT"], expected=expected,
           observations={}, failures=[], skips=[], started=now(), status="FAIL")
container = "proc-boot-id-fixed"
events = []
def scrub(text):
    return re.sub(r"(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", "[uuid]", text)
def runsc(*args, check=True, timeout=150):
    # Background Sentry and Gofer processes may inherit their standard fds.
    # Regular files do not hold subprocess.run open waiting for pipe EOF.
    event = {"operation": args[0], "started": now(), "timeout_seconds": timeout}
    events.append(event)
    stdout = bundle / ("op%d.stdout" % len(events))
    stderr = bundle / ("op%d.stderr" % len(events))
    command = [install/"runsc", "--root="+str(bundle/"runtime"), "--platform=systrap",
               "--network=none", "--overlay2=none", "--host-settings=ignore", "--ignore-cgroups", *args]
    try:
        with stdout.open("wb") as a, stderr.open("wb") as b:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=a, stderr=b, timeout=timeout,
                                    env=dict(os.environ, GVISOR_SIDECAR_BINARIES_DIR=str(install/"gvisor-bin")))
        event["exit"] = result.returncode
    except subprocess.TimeoutExpired:
        event["timed_out"] = True
        raise
    finally:
        event["finished"] = now()
        event["stderr"] = scrub(stderr.read_text(errors="replace")[-3500:])
    if check and result.returncode:
        raise RuntimeError((args[0], result.returncode, event["stderr"]))
    return stdout.read_text(errors="replace")

def sample(case, *args):
    data = json.loads(runsc("exec", "--cap=CAP_SYS_ADMIN", container, "/probe", *args))
    for key, n in (("boot",2),("repeated",2),("random",4)):
        assert len(data[key]) == n and all(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", v) for v in data[key]), "invalid "+key
    assert len(set(data["random"])) == 4 and data["boot"] == data["repeated"], "repeated reads"
    assert data["boot"][0] == data["boot"][1], "different IDs for two distinct proc mounts"
    out["observations"][case] = {"two_distinct_proc_mount_ids":True, "boot_ids_equal":True,
                                 "repeated_reads_stable":True, "four_distinct_random_uuids":True}
    return data["boot"][0], data["random"]

try:
    assert json.loads((proof/"fixed-build.json").read_text())["result"] == "PASS"
    bundle.mkdir()
    fs.mkdir()
    for m in ("proc", "second", "third"):
        (fs/m).mkdir()
    (bundle/"runtime").mkdir()
    image.mkdir()
    shutil.copy2(tmp/"probe-fixed", fs/"probe")
    spec = dict(ociVersion="1.0.2", root=dict(path="rootfs"),
                process=dict(terminal=False, user=dict(uid=0,gid=0), cwd="/", args=["/probe"], env=["PATH=/","GOMAXPROCS=2"],
                             capabilities={k:["CAP_SYS_ADMIN"] for k in ("bounding","effective","permitted")}),
                mounts=[dict(type="proc",source="proc",destination=p) for p in ("/proc","/second")],
                linux=dict(namespaces=[dict(type=t) for t in ("pid","mount","ipc","uts","network")]))
    (bundle/"config.json").write_text(json.dumps(spec))
    out["runsc_sha256"] = hashlib.file_digest((install/"runsc").open("rb"), "sha256").hexdigest()
    runsc("run","--detach","--bundle="+str(bundle),container)
    before, a = sample(expected[0], "/proc","/second")
    runsc("checkpoint","--compression=none","--image-path="+str(image),container)
    runsc("delete","--force",container)
    runsc("restore","--detach","--bundle="+str(bundle),"--image-path="+str(image),container)
    after, b = sample(expected[1], "/proc","/second")
    fresh, c = sample(expected[2], "--mount","/proc","/third")
    assert before == after == fresh, "boot ID changed across checkpoint or creating another proc mount"
    assert len(set(a+b+c)) == 12, "random UUID reused across observations"
    out.update(original_id_restored=True, post_restore_mount_shared_id=True, distinct_random_uuids=12, status="PASS")
except Exception as error:
    out["failures"].append(str(error))
finally:
    out["execution_finished_at"]=now()
    if (bundle/"runtime").exists():
        if out["status"] != "PASS":
            try:
                state = json.loads(runsc("state",container,timeout=10))
                out["runtime_state_before_cleanup"] = {k:state.get(k) for k in ("id","status","pid","ociVersion")}
            except Exception as error:
                out["runtime_state_error"] = str(error)[:1200]
        try:
            runsc("delete","--force",container,check=False,timeout=15)
        except Exception as error:
            out["cleanup_error"] = str(error)[:1200]
    out["runsc_operations"] = events
    out["missing"]=sorted(set(expected)-set(out["observations"]))
    out["verdict_available_at"]=now()
    (proof/"restore.json").write_text(json.dumps(out,indent=2)+"\n")
if out["status"] != "PASS":
    raise SystemExit(1)
