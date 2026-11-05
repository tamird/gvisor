"""Compare local background restore on the same binary and checkpoint."""
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import itertools
import select
import sys
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
pairs = 18
modes = ("control", "willneed", "buffer")
orders = list(itertools.permutations(modes))
order = [(p, condition, mode) for p in range(pairs)
         for condition in (("warm", "cold") if p % 2 == 0 else ("cold", "warm"))
         for mode in orders[(p + (3 if condition == "cold" else 0)) % len(orders)]]
def key(p, c, mode):
    return f"{p:02}-{c}-{mode}"

cid = "restore-advice"
image = root / "image"
rows = []
summary = dict(source=src, base=base, expected=[key(*x) for x in order], attempted=[], executed=[], missing=[], failures=[], skips=[], result="FAIL")
libc = ctypes.CDLL(None, use_errno=True)
page_size = os.sysconf("SC_PAGE_SIZE")

def persist():
    summary["missing"] = sorted(set(summary["expected"]) - set(summary["executed"]))
    (out/"samples.json").write_text(json.dumps(rows, indent=2) + "\n")
    (out/"summary.json").write_text(json.dumps(summary, indent=2) + "\n")

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
    # UID 0 is already mapped to the single unprivileged outer UID. Restore
    # accepts ordinary namespace-root operation and rejects the --rootless mode.
    cmd = [str(install/"runsc"), "--root="+str(root/"runtime"), "--platform=systrap", "--network=none", "--overlay2=none", "--host-settings=ignore", "--ignore-cgroups", *args]
    env = dict(os.environ, GVISOR_SIDECAR_BINARIES_DIR=str(install/"gvisor-bin"))
    if mode is not None:
        env["GVISOR_FADVISE_TRIAL"] = mode
    with tempfile.TemporaryFile(mode="w+") as so, tempfile.TemporaryFile(mode="w+") as se:
        t = time.perf_counter_ns()
        # subprocess.wait(timeout) on this Python polls in 50 ms steps. A
        # pidfd becomes readable on process exit and still bounds a hung run.
        with subprocess.Popen(cmd, env=env, stdout=so, stderr=se, stdin=subprocess.DEVNULL) as pr:
            pidfd = os.pidfd_open(pr.pid)
            try:
                poll = select.poll()
                poll.register(pidfd, select.POLLIN)
                if not poll.poll(180_000):
                    pr.kill()
                    raise subprocess.TimeoutExpired(cmd,180)
                pr.wait()
            finally:
                os.close(pidfd)
        elapsed = (time.perf_counter_ns() - t)/1e6
        so.seek(0); se.seek(0); a,b = so.read().strip(),se.read().strip()
    if check and pr.returncode:
        summary["command_failure"] = {
            "operation": args[0], "exit": pr.returncode, "mode": mode,
            "stderr_sha256": hashlib.sha256(b.encode()).hexdigest(),
            "diagnostics": [term for term in (
                "operation not permitted", "permission denied", "no such file or directory",
                "read-only file system", "mount", "setup root", "unshare", "seccomp",
                "mapping", "connection reset by peer", "failed to load kernel",
                "statefile", "systrap", "out of memory", "timed out",
                "rootless mode not supported", "cannot create", "invalid argument",
                "cannot run", "input/output error", "capability", "user namespace",
                "creating sandbox", "starting sandbox", "creating gofer", "restoring",
            ) if term in b.lower()],
        }
        # Only the known test capsule is visible here. Retain one bounded runtime
        # error, excluding addresses, URLs, environmental values, and log dumps.
        for line in b.splitlines():
            if re.match(r"(?i)^(?:rootless|running|creating|starting|restoring|loading|opening|sandbox|error|failed|setting up)\b", line):
                if not re.search(r"(?i)://|authorization|credential|token|password|[A-Za-z_][A-Za-z_0-9]*=", line):
                    line = re.sub(r"0x[0-9a-fA-F]+", "<address>", line)
                    summary["command_failure"]["first_error"] = re.sub(r"[^ -~]", "", line)[:400]
                    break
        raise RuntimeError(f"{args[0]} exited {pr.returncode}")
    if mode is not None:
        matches=re.findall(r"^RESTORE-TRIAL (\w+) (\d+)$", b, re.M)
        if len(matches)!=1 or matches[0][0]!=mode:
            raise ValueError(("RPC did not return requested reader",mode))
        kernel_ms=int(matches[0][1])/1e6
        assert 0 < kernel_ms < elapsed, (kernel_ms,elapsed)
        return a,elapsed,kernel_ms
    return a,elapsed

def remove():
    call("delete", "--force", cid, check=False)

try:
    summary["phase"] = "private-root"
    persist()
    assert os.getuid() == 0 and not Path("/var/run/docker.sock").exists()
    summary["uid_map"] = Path("/proc/self/uid_map").read_text().split()
    assert summary["uid_map"] == ["0", os.environ["OUTER_UID"], "1"]
    root.mkdir(); image.mkdir(); (root/"runtime").mkdir()
    guest = root/"rootfs"; guest.mkdir(); shutil.copy2(tmp/"advice-guest",guest/"probe")
    def digest(path):
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    summary.update(kernel=os.uname().release, cpus=len(os.sched_getaffinity(0)), python=sys.version.split()[0])
    summary["binaries"] = {p.name: digest(p) for p in [guest/"probe", install/"runsc"]}
    spec = dict(ociVersion="1.0.2", root=dict(path="rootfs"), process=dict(terminal=False,user=dict(uid=0,gid=0),cwd="/",args=["/probe"],env=["PATH=/","GOMAXPROCS=2"]), mounts=[dict(type="proc",source="proc",destination="/proc")], linux=dict(namespaces=[dict(type=t) for t in ("pid","mount","ipc","uts","network")]))
    (root/"config.json").write_text(json.dumps(spec))
    summary["phase"] = "guest-start"
    persist()
    call("run","--detach","--bundle="+str(root),cid)
    ready = ""
    for i in range(60):
        ready,_ = call("exec",cid,"/probe","ready",check=False)
        if re.fullmatch(r"[a-f0-9]{64} [a-f0-9]{64}", ready): break
        time.sleep(.25)
    assert re.fullmatch(r"[a-f0-9]{64} [a-f0-9]{64}",ready), "guest never filled its pipes and memory"
    summary["expected_digests"] = ready
    summary["phase"] = "checkpoint"
    persist()
    call("checkpoint","--compression=none","--image-path="+str(image),cid)
    remove()
    files = [image/n for n in ("checkpoint.img","pages.img","pages_meta.img")]
    summary["files"] = {p.name:p.stat().st_size for p in files}
    frame_report = subprocess.check_output([tmp/"inspect", files[0]], text=True, timeout=60)
    frames = re.fullmatch(r"cpuid (\d+)\nkernel (\d+)\n", frame_report)
    assert frames and all(int(length) > 0 for length in frames.groups()), "missing graph frame"
    summary["graph_frames"] = dict(zip(("cpuid", "kernel"), map(int, frames.groups())))
    summary["compression"] = "none"
    summary["checkpoint_sha256"] = {path.name: digest(path) for path in files}
    assert all(size > 0 for size in summary["files"].values()), summary["files"]
    summary["phase"] = "restore"
    persist()
    for p, condition, mode in order:
        name = key(p,condition,mode); summary["attempted"].append(name)
        r = dict(case=name,pair=p,condition=condition,mode=mode)
        try:
            r["residency"] = cache(files,condition == "warm")
            _,r["restore_cli_ms"],r["kernel_load_ms"] = call("restore","--detach","--background","--bundle="+str(root),"--image-path="+str(image),cid,mode=mode)
            response,r["ready_cli_ms"] = call("exec",cid,"/probe","ready")
            assert response == ready,response
            verified,_ = call("exec",cid,"/probe","verify")
            assert verified == ready,verified
            r["bytes_and_pipe_digests_match"] = True
            summary["executed"].append(name)
        finally:
            r["finished_at"]=now(); rows.append(r); persist(); remove()
    summary["execution_finished_at"]=rows[-1]["finished_at"]
    assert len(summary["executed"]) == len(set(summary["expected"]))
    assert {path.name: digest(path) for path in files} == summary["checkpoint_sha256"]
    metrics = ("kernel_load_ms","restore_cli_ms")
    summary["medians_ms"] = {metric:{c:{m:statistics.median(r[metric] for r in rows if r["condition"]==c and r["mode"]==m) for m in modes} for c in ("warm","cold")} for metric in metrics}
    for mode in modes:
        raw = ["BenchmarkRestore/%s 1 %.3f kernel-ms/op %.3f cli-ms/op" % (r["condition"],r["kernel_load_ms"],r["restore_cli_ms"]) for r in rows if r["mode"] == mode]
        (out/(mode+".bench")).write_text("\n".join(raw)+"\n")
    summary["phase"]="verified"
    summary["result"]="PASS"
except BaseException as e:
    summary["failures"].append(dict(case=summary["attempted"][-1] if summary["attempted"] else "setup",
                                    error=type(e).__name__, errno=getattr(e, "errno", None)))
    raise
finally:
    summary.setdefault("execution_finished_at",now())
    summary["missing"]=sorted(set(summary["expected"])-set(summary["executed"]))
    if install.exists() and root.exists():
        try:
            remove()
        except (OSError, subprocess.SubprocessError):
            pass
    summary["verdict_available_at"]=now()
    persist()
    print(json.dumps({k:v for k,v in summary.items() if k not in {"expected","executed","attempted","missing"}}))
