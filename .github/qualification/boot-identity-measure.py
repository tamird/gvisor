"""Read two distinct proc mounts and restore the same unread checkpoint twice."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

tmp = Path(os.environ["RUNNER_TEMP"])
arm = sys.argv[1]
proof = tmp / "boot-identity-results"
root = tmp / ("bundle-" + arm)
install = tmp / ("install-" + arm)
program = tmp / ("probe-" + arm)
image = root / "image"
container = "boot-identity"
expected = ["first restore", "second restore", "live read", "read before restore"] if arm != "linux" else ["native mounts"]
def now():
    return datetime.now(timezone.utc).isoformat()
summary = dict(expected=expected, executed=[], skips=[], source=os.environ["SOURCE"], observations={}, failures=[], status="FAIL")
def run(cmd, *, quiet=False, required=True, env=None):
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90, env=env)
    if required and result.returncode:
        raise RuntimeError((cmd[1:3], result.returncode, result.stderr[-1800:]))
    return result.stdout
def runsc(*args, required=True):
    return run([str(install/"runsc"), "--root="+str(root/"runtime"), "--platform=systrap", "--network=none", "--overlay2=none", "--host-settings=ignore", "--ignore-cgroups", *args], env=dict(os.environ, GVISOR_SIDECAR_BINARIES_DIR=str(install/"gvisor-bin")), required=required)
def remove():
    runsc("delete","--force",container,required=False)
def record(case, output):
    d=json.loads(output)
    for key, n in (("boot",2),("repeated",2),("random",4)):
        assert len(d[key]) == n and all(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",x) for x in d[key]), "invalid UUID format or count"
    assert d["boot"]==d["repeated"] and len(set(d["random"]))==4, "per-mount stability or fresh UUID control failed"
    summary["observations"][case]={"same_boot_across_mounts":d["boot"][0]==d["boot"][1],"repeated_boot_stable":True,"distinct_random_uuids":True,"distinct_proc_mounts":True}
    summary["executed"].append(case)
    return d["boot"]
try:
    root.mkdir()
    if arm=="linux":
        (root/"first").mkdir(); (root/"second").mkdir()
        for directory in [root/"first",root/"second"]:
            run(["mount","-t","proc","proc",str(directory)])
        value=record("native mounts",run([tmp/"probe-stock",root/"first",root/"second"]))
        assert value[0]==value[1], "Linux did not share boot ID"
    else:
        build=json.loads((proof/(arm+"-build.json")).read_text())
        assert build["result"]=="PASS" and build["source"]==summary["source"]
        (root/"runtime").mkdir(); image.mkdir()
        fs=root/"rootfs";fs.mkdir();(fs/"proc").mkdir();(fs/"second").mkdir();shutil.copy2(program,fs/"probe")
        summary["runsc_sha256"]=hashlib.file_digest((install/"runsc").open("rb"),"sha256").hexdigest()
        spec=dict(ociVersion="1.0.2",root=dict(path="rootfs"),process=dict(terminal=False,user=dict(uid=0,gid=0),cwd="/",args=["/probe"],env=["GOMAXPROCS=2","PATH=/"]),mounts=[dict(type="proc",source="proc",destination=p) for p in ("/proc","/second")],linux=dict(namespaces=[dict(type=t) for t in ("pid","mount","ipc","uts","network")]))
        (root/"config.json").write_text(json.dumps(spec))
        def checkpoint():
            runsc("checkpoint","--compression=none","--image-path="+str(image),container);remove()
        def restore():
            runsc("restore","--detach","--bundle="+str(root),"--image-path="+str(image),container)
        def sample(name):
            return record(name,runsc("exec",container,"/probe","/proc","/second"))
        runsc("run","--detach","--bundle="+str(root),container)
        checkpoint()
        restore(); first=sample(expected[0]);remove()
        restore(); second=sample(expected[1])
        summary["same_for_each_mount_across_two_unread_restores"]=[a==b for a,b in zip(first,second)]
        before=sample(expected[2])
        image=root/"observed-image";image.mkdir()
        checkpoint();restore();after=sample(expected[3])
        assert second==before and before==after, "previously read IDs changed"
        summary["previously_read_ids_restored"]=True
    summary["execution_finished_at"]=now()
    summary["status"]="PASS"
except BaseException as err:
    summary["failures"].append(str(err));raise
finally:
    if arm!="linux" and install.exists() and (root/"runtime").exists():
        remove()
    summary.setdefault("execution_finished_at",now())
    summary["missing"]=sorted(set(expected)-set(summary["executed"]))
    summary["verdict_available_at"]=now()
    (proof/(arm+".json")).write_text(json.dumps(summary,indent=2)+"\n")
