"""Compare local background restore on the same binary and checkpoint."""
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import tempfile
import time

def now():
    return datetime.now(timezone.utc).isoformat()

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "restore-advice-results"
out.mkdir(exist_ok=True)
root = tmp / "advice-bundle"
install = tmp / "advice-install"
src = os.environ["SOURCE"]
base = os.environ["BASE"]
pairs = 16
order = [(p, condition, mode) for p in range(pairs)
         for condition in (("warm", "cold") if p % 2 == 0 else ("cold", "warm"))
         for mode in (("control", "willneed") if (p // 2 + (condition == "cold")) % 2 == 0 else ("willneed", "control"))]
def key(p, c, mode):
    return f"{p:02}-{c}-{mode}"

cid = "restore-advice"
image = root / "image"
rows = []
summary = dict(source=src, base=base, expected=[key(*x) for x in order], attempted=[], executed=[], missing=[], failures=[], skips=[], result="FAIL")
libc = ctypes.CDLL(None, use_errno=True)
page_size = os.sysconf("SC_PAGE_SIZE")

def cache(files, warm):
    observed = {}
    for path in files:
        with path.open('rb') as f:
            os.fsync(f.fileno())
            if warm:
                while f.read(4 << 20): pass
            else:
                os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            size = os.fstat(f.fileno()).st_size
            n = (size + page_size - 1) // page_size
            vec = (ctypes.c_ubyte * n)()
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_COPY) as m:
                v = ctypes.c_char.from_buffer(m)
                if libc.mincore(ctypes.c_void_p(ctypes.addressof(v)), ctypes.c_size_t(size), vec) == -1:
                    raise OSError(ctypes.get_errno(), "mincore")
                del v
            observed[path.name] = sum(x & 1 for x in vec) / n
    if not all(r >= .99 if warm else r <= .01 for r in observed.values()):
        raise ValueError(f"page-cache condition not established: {observed}")
    return observed

def call(*args, mode=None, check=True):
    cmd = [str(install/"runsc"), "--root="+str(root/"runtime"), "--platform=systrap", "--network=none", "--overlay2=none", "--host-settings=ignore", "--ignore-cgroups", *args]
    env = dict(os.environ, GVISOR_SIDECAR_BINARIES_DIR=str(install/"gvisor-bin"))
    if mode is not None:
        env["GVISOR_FADVISE_TRIAL"] = mode
    with tempfile.TemporaryFile(mode="w+") as so, tempfile.TemporaryFile(mode="w+") as se:
        t = time.perf_counter_ns()
        pr = subprocess.run(cmd, env=env, stdout=so, stderr=se, stdin=subprocess.DEVNULL, timeout=180)
        elapsed = (time.perf_counter_ns() - t)/1e6
        so.seek(0); se.seek(0); a,b = so.read().strip(),se.read().strip()
    if check and pr.returncode: raise RuntimeError((args,pr.returncode,a[-1000:],b[-2000:]))
    if mode is not None and ("FADVISE-TRIAL " + mode) not in b.splitlines():
        raise ValueError(("restore did not acknowledge selected mode", mode, b[-1000:]))
    return a,elapsed

def remove():
    call("delete", "--force", cid, check=False)

try:
    build = json.loads((out/"build.json").read_text())
    assert build["source"] == src and build["result"] == "PASS", build
    root.mkdir(); image.mkdir(); (root/"runtime").mkdir()
    guest = root/"rootfs"; guest.mkdir(); shutil.copy2(tmp/"advice-guest",guest/"probe")
    def digest(path):
        with path.open("rb") as f: return hashlib.file_digest(f, "sha256").hexdigest()
    summary["binaries"] = {p.name: digest(p) for p in [guest/"probe", install/"runsc"]}
    spec = dict(ociVersion="1.0.2", root=dict(path="rootfs"), process=dict(terminal=False,user=dict(uid=0,gid=0),cwd="/",args=["/probe"],env=["PATH=/","GOMAXPROCS=2"]), mounts=[dict(type="proc",source="proc",destination="/proc")], linux=dict(namespaces=[dict(type=t) for t in ("pid","mount","ipc","uts","network")]))
    (root/"config.json").write_text(json.dumps(spec))
    call("run","--detach","--bundle="+str(root),cid)
    ready = ""
    for i in range(60):
        ready,_ = call("exec",cid,"/probe","ready",check=False)
        if re.fullmatch(r"[a-f0-9]{64} [a-f0-9]{64}", ready): break
        time.sleep(.25)
    assert re.fullmatch(r"[a-f0-9]{64} [a-f0-9]{64}",ready), "guest never filled its pipes and memory"
    summary["expected_digests"] = ready
    call("checkpoint","--compression=none","--image-path="+str(image),cid)
    remove()
    files = [image/n for n in ("checkpoint.img","pages.img","pages_meta.img")]
    summary["files"] = {p.name:p.stat().st_size for p in files}
    assert files[0].stat().st_size >= (8 << 20) and files[1].stat().st_size >= (512 << 20), summary["files"]
    for p, condition, mode in order:
        name = key(p,condition,mode); summary["attempted"].append(name)
        r = dict(case=name,pair=p,condition=condition,mode=mode)
        try:
            r["residency"] = cache(files,condition == "warm")
            _,r["restore_cli_ms"] = call("restore","--detach","--background","--bundle="+str(root),"--image-path="+str(image),cid,mode=mode)
            response,r["ready_cli_ms"] = call("exec",cid,"/probe","ready")
            assert response == ready,response
            verified,_ = call("exec",cid,"/probe","verify")
            assert verified == ready,verified
            r["bytes_and_pipe_digests_match"] = True
            summary["executed"].append(name)
        finally:
            r["finished_at"]=now(); rows.append(r); remove()
    summary["execution_finished_at"]=rows[-1]["finished_at"]
    assert len(summary["executed"]) == len(set(summary["expected"]))
    summary["medians_ms"] = {c:{m:statistics.median(r["restore_cli_ms"] for r in rows if r["condition"]==c and r["mode"]==m) for m in ("control","willneed")} for c in ("warm","cold")}
    for mode in ("control","willneed"):
        raw = []
        for row in rows:
            if row["mode"] == mode:
                raw.append("BenchmarkRestore/"+row["condition"]+" 1 "+("%.3f" % row["restore_cli_ms"])+" restore-ms/op")
        (out/(mode+".bench")).write_text("\n".join(raw)+"\n")
    summary["result"]="PASS"
except BaseException as e:
    summary["failures"].append(dict(case=summary["attempted"][-1] if summary["attempted"] else "setup",error=repr(e)))
    raise
finally:
    summary.setdefault("execution_finished_at",now())
    summary["missing"]=sorted(set(summary["expected"])-set(summary["executed"]))
    if install.exists() and root.exists(): remove()
    (out/"samples.json").write_text(json.dumps(rows,indent=2)+"\n")
    summary["verdict_available_at"]=now()
    (out/"summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps({k:v for k,v in summary.items() if k not in {"expected","executed","attempted","missing"}}))
