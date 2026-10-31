"""Exercise the installed releases on the native runner using actual saved processes."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

tmp = Path(os.environ["RUNNER_TEMP"])
proof = tmp / "proof"
install = tmp / "identity-release"
records = json.loads((proof / "builds.json").read_text())
expected = {"original-A/original-B":"running","prototype-A/prototype-A":"running",
            "prototype-A/prototype-B":"mismatch", "original-A/prototype-A":"mismatch"}
observed = {}
def pending():
    return dict(expected=expected,executed=sorted(observed),missing=sorted(expected.keys()-observed.keys()),
                failures=[k for k,v in observed.items() if v.get("observed")!=expected[k]],skips=[])
def persist_partial():
    (proof/"verdict.json").write_text(json.dumps(pending(),indent=2)+"\n")
persist_partial()
def now():
    return datetime.now(timezone.utc).isoformat()
def select(name):
    # Replace the inode. Never rewrite an executable which is running.
    target = install/"gvisor-bin/gvisor_sentry"
    staged = target.with_name("selected-sentry")
    shutil.copy2(tmp/(name+".sentry"),staged)
    os.replace(staged,target)
with tempfile.TemporaryDirectory(prefix="sentry-id-",dir="/tmp") as work:
    root = Path(work)
    guest = root/"rootfs"
    guest.mkdir()
    shutil.copy2(tmp/"identity-guest",guest/"probe")
    spec = dict(ociVersion="1.0.2",root={"path":"rootfs"},process=dict(terminal=False,user={"uid":0,"gid":0},
        cwd="/",args=["/probe","task-tree","--depth=0"],env=["PATH=/"]),mounts=[{"type":"proc","source":"proc","destination":"/proc"}],
        linux={"namespaces":[{"type":t} for t in ("pid","mount","ipc","uts","network")]})
    (root/"config.json").write_text(json.dumps(spec))
    state = root/"runtime"
    state.mkdir()
    command = [str(install/"runsc"),"--root="+str(state),"--platform=systrap","--network=none",
               "--overlay2=none","--host-settings=ignore","--ignore-cgroups"]
    def call(*args,check=True):
        # The detached guest inherits these descriptors from runsc. Waiting
        # for pipe EOF would wait for the guest as well as the CLI process.
        with tempfile.TemporaryFile(mode="w+") as out, tempfile.TemporaryFile(mode="w+") as err:
            result = subprocess.run(command+list(args),stdout=out,stderr=err,timeout=90,stdin=subprocess.DEVNULL)
            out.seek(0); err.seek(0)
            result.stdout, result.stderr = out.read(), err.read()
        if check and result.returncode:
            raise RuntimeError((args,result.returncode,result.stdout,result.stderr))
        return result
    cid = "identity-guest"
    for prod, consumers in (("original-A",("original-B","prototype-A")),("prototype-A",("prototype-A","prototype-B"))):
        image = root/("image-"+prod)
        image.mkdir()
        select(prod)
        call("run","--detach","--bundle="+str(root),cid)
        call("checkpoint","--image-path="+str(image),cid)
        assert any(p.stat().st_size for p in image.iterdir())
        call("delete","--force",cid)
        for cons in consumers:
            select(cons)
            name = prod+"/"+cons
            r = call("restore","--detach","--bundle="+str(root),"--image-path="+str(image),cid,check=False)
            try:
                result = dict(exit=r.returncode,stdout=r.stdout,stderr=r.stderr)
                if r.returncode == 0:
                    state_info = json.loads(call("state",cid).stdout)
                    assert state_info["status"] == "running",state_info
                    live = call("exec",cid,"/probe","echo","restored-"+name)
                    assert live.stdout.strip()=="restored-"+name,live
                    result["observed"] = "running"
                elif "version does not match across checkpoint restore" in r.stderr+r.stdout:
                    assert "go-buildid:"+records[cons]["go_id"] in r.stderr+r.stdout,(name,r.stderr)
                    if prod != "original-A":
                        assert "go-buildid:"+records[prod]["go_id"] in r.stderr+r.stdout,(name,r.stderr)
                    result["observed"] = "mismatch"
                else:
                    result["observed"] = "unexpected"
                observed[name] = result
                persist_partial()
                (proof/"restores.json").write_text(json.dumps(observed,indent=2)+"\n")
            finally:
                call("delete","--force",cid,check=False)
finished = now()
missing = sorted(expected.keys()-observed.keys())
failures = [key for key,got in observed.items() if got.get("observed")!=expected[key]]
verdict = dict(expected=expected,executed=sorted(observed),missing=missing,failures=failures,skips=[],
               execution_finished=finished,verified=now())
(proof/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
assert not (missing or failures),verdict
print(json.dumps(verdict,indent=2))
