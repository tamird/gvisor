"""Explicit graph-engine checkpoint and restore on the installed unversioned runtime."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
tmp = Path(os.environ["RUNNER_TEMP"])
proof = tmp / "owner-identity-proof"
proof.mkdir(exist_ok=True)
root = tmp/"owner-graph-sandbox"
root.mkdir()
install = tmp/"owner-identity-release"
cid = "graph-identity"
flags = [str(install/"runsc"),"--root="+str(root/"runtime"),"--platform=systrap","--network=none",
         "--overlay2=none","--host-settings=ignore","--ignore-cgroups","--state-engine=graph"]
record = dict(result="FAIL",source=os.environ['SOURCE'],engine="graph",steps=[])
def call(*args,check=True):
    with tempfile.TemporaryFile(mode="w+") as out, tempfile.TemporaryFile(mode="w+") as err:
        p=subprocess.run([*flags,*args],stdout=out,stderr=err,stdin=subprocess.DEVNULL,timeout=90)
        out.seek(0);err.seek(0)
        stdout,stderr=out.read(),err.read()
    record["steps"].append(dict(argv=list(args),exit=p.returncode,stdout=stdout[-2560:],stderr=stderr[-2560:]))
    if p.returncode and check: raise RuntimeError((args,p.returncode,stderr))
    return stdout
try:
    built=json.loads((proof/"build-amd64.json").read_text())
    assert built["result"]=="PASS"
    with (install/"gvisor-bin/gvisor_sentry").open("rb") as f:
        assert hashlib.file_digest(f,"sha256").hexdigest()==built["sha256"]
    record["sentry_sha256"]=built["sha256"]
    guest=root/"rootfs"; guest.mkdir()
    shutil.copy2(tmp/"owner-identity-guest",guest/"probe")
    image=root/"image"; image.mkdir()
    (root/"runtime").mkdir()
    spec=dict(ociVersion="1.0.2",root={"path":"rootfs"},process=dict(terminal=False,user={"uid":0,"gid":0},
        cwd="/",args=["/probe","task-tree","--depth=0"],env=["PATH=/"]),mounts=[dict(type="proc",source="proc",destination="/proc")],
        linux={"namespaces":[{"type":t} for t in ("pid","mount","ipc","uts","network")]})
    (root/"config.json").write_text(json.dumps(spec))
    call("run","--detach","--bundle="+str(root),cid)
    call("checkpoint","--image-path="+str(image),cid)
    record["state_files"]={f.name:f.stat().st_size for f in image.iterdir() if f.is_file()}
    assert any(record["state_files"].values()), record
    call("delete","--force",cid)
    call("restore","--detach","--bundle="+str(root),"--image-path="+str(image),cid)
    assert json.loads(call("state",cid))["status"]=="running"
    assert call("exec",cid,"/probe","echo","restored-graph").strip()=="restored-graph"
    record["result"]="PASS"
except BaseException as e:
    record["error"]=repr(e); raise
finally:
    if (install/"runsc").is_file():
        call("delete","--force",cid,check=False)
    record["recorded"]=datetime.now(timezone.utc).isoformat()
    (proof/"graph.json").write_text(json.dumps(record,indent=2)+"\n")
    print(json.dumps(record,indent=2))
