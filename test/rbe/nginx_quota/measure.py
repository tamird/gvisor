"""Live Nginx restore under verified CPU budgets on one native Linux worker."""
import ctypes
import datetime
import hashlib
import itertools
import json
import mmap
import os
from pathlib import Path
import re
import select
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

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


source = "d3066814d48a526561ab14dd6cd9c820182a79cf"
base = "63d3761ddffe329a13d142d91dd61cceeec76fcd"
quotas = ("0.25", "1", "2", "4", "16", "max")
modes = ("control", "willneed", "buffer")
cycles = list(itertools.permutations(modes))
samples = []
captures = {}
out = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
work = Path(tempfile.mkdtemp(prefix="nginx-quota-", dir="/tmp"))
cid = "quota-reader"
out.mkdir(exist_ok=True, parents=True)
files = ("checkpoint.img", "pages_meta.img", "pages.img")
release = work / "release"
env = dict(os.environ, GVISOR_SIDECAR_BINARIES_DIR=str(release / "gvisor-bin"))
cli = [str(release / "runsc"), "--root="+str(work/"runtime"), "--platform=systrap",
       "--network=none", "--overlay2=none", "--host-settings=ignore"]
template = {}
worker_cpus = len(os.sched_getaffinity(0))


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest(path):
    m = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            m.update(chunk)
    return m.hexdigest()


def call(*args, mode=None, check=True):
    with tempfile.TemporaryFile() as so, tempfile.TemporaryFile() as se:
        start = time.monotonic_ns()
        with subprocess.Popen([*cli, *map(str,args)], stdout=so, stderr=se,
                              stdin=subprocess.DEVNULL,
                              env=dict(env, **({"GVISOR_FADVISE_TRIAL":mode} if mode else {}))) as p:
            fd = os.pidfd_open(p.pid)
            try:
                poll = select.poll()
                poll.register(fd, select.POLLIN)
                if not poll.poll(120_000):
                    p.kill()
                    raise TimeoutError(args[:3])
                p.wait()
            finally:
                os.close(fd)
        elapsed = (time.monotonic_ns()-start)/1e6
        so.seek(0); se.seek(0)
        stdout, stderr = so.read(), se.read()
        if p.returncode and check:
            raise RuntimeError((args[:4],p.returncode,stderr.decode(errors="replace")[-1500:]))
        if mode:
            matches = re.findall(rb"^RESTORE-TRIAL (\w+) (\d+)$", stderr, re.M)
            assert len(matches)==1 and matches[0][0].decode()==mode, (mode,stderr[-250:])
            kernel = int(matches[0][1])/1e6
            assert 0 < kernel < elapsed
            return elapsed, kernel
        return stdout, elapsed


def configure(quota):
    template["linux"]["cgroupsPath"] = "/gv-restore-quota-" + quota.replace(".","p")
    resources = template["linux"]["resources"]
    if quota == "max":
        resources.pop("cpu",None)
    else:
        resources["cpu"] = {"period":100000,"quota":int(float(quota)*100000)}
    (work/"config.json").write_text(json.dumps(template))


def cgroup(quota):
    pid = json.loads(call("state",cid)[0])["pid"]
    cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    cpu_idx = cmd.index(b"--cpu-num")
    ncpu = int(cmd[cpu_idx+1])
    assert ncpu == (worker_cpus if quota=="max" else min(worker_cpus,max(2,int(float(quota))))), (quota,ncpu,worker_cpus)
    entries = [line.split(":",2) for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()]
    mounts = [line.split() for line in Path("/proc/mounts").read_text().splitlines()]
    def path_for(kind):
        paths = [p for _,ctrls,p in entries if kind in ctrls.split(",")]
        if not paths:
            return None
        bases = [m[1] for m in mounts if m[2]=="cgroup" and kind in m[3].split(",")]
        assert len(paths)==len(bases)==1,(kind,paths,bases)
        return Path(bases[0])/paths[0].lstrip("/")
    cpu = path_for("cpu")
    if cpu is None:
        root = next(Path(m[1]) for m in mounts if m[2]=="cgroup2")
        cpu = root/next(p.lstrip("/") for _,ctrls,p in entries if not ctrls)
        charge = cpu/"cpu.stat"
        cap = cpu/"cpu.max"
        def get_cap(p):
            bound, period = p.read_text().split()
            return float("inf") if bound=="max" else int(bound)/int(period)
    else:
        root = next(Path(m[1]) for m in mounts if m[2]=="cgroup" and "cpu" in m[3].split(","))
        charge = path_for("cpuacct")/"cpuacct.usage"
        cap = cpu/"cpu.cfs_quota_us"
        def get_cap(p):
            bound=int(p.read_text())
            return float("inf") if bound<0 else bound/int((p.parent/"cpu.cfs_period_us").read_text())
    assert cpu.name==template["linux"]["cgroupsPath"].split("/")[-1], (quota,str(cpu))
    want = float("inf") if quota=="max" else float(quota)
    assert get_cap(cap)==want,(quota,cap.read_text())
    limits = [get_cap(p/cap.name) for p in [cpu,*cpu.parents] if (p==root or root in p.parents) and (p/cap.name).exists()]
    assert min(limits) >= (16 if quota=="max" else want),(quota,limits)
    return (charge,cpu/"cpu.stat"), {"sentry_cpus":ncpu,"buffer_parallel_reads":min(8,max(1,ncpu)),
                    "leaf_quota":quota,"observed_caps":[None if x==float("inf") else x for x in limits]}


def cpu_ms(path):
    if path.name=="cpuacct.usage":
        return int(path.read_text())/1e6
    return int(dict(line.split() for line in path.read_text().splitlines())["usage_usec"])/1e3


def throttled_ms(path):
    data=dict(line.split() for line in path.read_text().splitlines())
    return int(data["throttled_usec"])/1e3 if "throttled_usec" in data else int(data["throttled_time"])/1e6


def http(n):
    url="http://127.0.0.1/latin1k.txt"
    raw,elapsed=call("exec",cid,"/usr/bin/curl","--noproxy","*","--fail","--silent","--show-error",
                     "--max-time","10","--write-out","HTTP:%{http_code}:%{time_total}\n",*([url]*n))
    matches = re.findall(rb"\x00{1024}HTTP:200:([0-9.]+)\n", raw)
    assert len(matches)==n and not re.sub(rb"\x00{1024}HTTP:200:([0-9.]+)\n",b"",raw), (len(raw),n,raw[-60:])
    wire=sum(float(t) for t in matches)
    assert wire>0
    return elapsed, n/wire


def delete():
    call("delete","--force",cid,check=False)


def capture(quota):
    configure(quota)
    image = work/("capture-"+quota)
    image.mkdir()
    call("run","--detach","--bundle",work,cid)
    end=time.monotonic()+30
    while True:
        try:
            http(1)
            break
        except (AssertionError,RuntimeError):
            if time.monotonic()>end: raise
            time.sleep(.05)
    tasks,_=call("exec",cid,"/bin/sh","-c",
                 """for f in /proc/[0-9]*/cmdline; do tr '\\000' ' ' < "$f"; echo; done""")
    workers = sum(line.startswith(b"nginx: worker process") for line in tasks.splitlines())
    assert workers==4,("Nginx workers",workers)
    _,bounds=cgroup(quota)
    call("checkpoint","--compression=none","--image-path",image,cid)
    delete()
    hashes={name:{"bytes":(image/name).stat().st_size,"sha256":digest(image/name)} for name in files}
    assert all(info["bytes"]>0 for info in hashes.values())
    captures[quota]=dict(files=hashes,workers=workers,**bounds)


def case(roundidx,quota,condition,mode):
    configure(quota)
    image=work/("capture-"+quota)
    r=dict(round=roundidx,quota=quota,condition=condition,reader=mode)
    samples.append(r)
    try:
        r["residency"]=cache([image/n for n in files],condition=="warm")
        restore_start=time.monotonic_ns()
        r["cli_ms"],r["kernel_ms"]=call("restore","--detach","--background","--bundle",work,"--image-path",image,cid,mode=mode)
        r["first_http_exec_ms"],_=http(1)
        r["restore_to_http_ms"]=(time.monotonic_ns()-restore_start)/1e6
        (charge,throttle),bounds=cgroup(quota)
        r.update(bounds)
        r["cpu_through_first_http_and_state_ms"]=cpu_ms(charge)
        r["throttled_through_first_http_and_state_ms"]=throttled_ms(throttle)
        r["burst_exec_ms"],r["curl_response_rps"]=http(16)
        r["burst_throttled_ms"]=throttled_ms(throttle)-r["throttled_through_first_http_and_state_ms"]
        r["burst_cpu_ms"]=cpu_ms(charge)-r["cpu_through_first_http_and_state_ms"]
        r["http_responses"]=17
        r["result"]="PASS"
    finally:
        r["finished_at"]=now()
        delete()


def main():
    global template
    assert os.geteuid()==0
    assert worker_cpus>=16, f"Only {worker_cpus} CPUs available"
    for tar,dest in ((sys.argv[2],work),(sys.argv[1],work/"rootfs")):
        dest.mkdir(exist_ok=True)
        subprocess.run(["tar","xf",tar,"-C",str(dest)],check=True)
    manifest=json.loads(Path(sys.argv[3]).read_text())
    assert len(manifest)==8 and all(digest(work/name)==f["digest"] for name,f in manifest.items())
    assert (work/"rootfs/usr/bin/curl").exists(),"Pinned Nginx image needs its existing curl client"
    call("spec","--bundle",work)
    template=json.loads((work/"config.json").read_text())
    template["root"].update(path="rootfs",readonly=False)
    template["process"].update(terminal=False,cwd="/",args=["/usr/sbin/nginx","-c","/etc/nginx/nginx_gofer.conf","-g","worker_processes 4;"])
    # Match the image's ordinary container privileges needed when the Nginx master
    # changes worker credentials and initializes the configured cache directories.
    for name in ("bounding","effective","permitted","inheritable"):
        caps=template["process"]["capabilities"][name]
        caps.extend(c for c in ("CAP_SETUID","CAP_SETGID","CAP_CHOWN","CAP_DAC_OVERRIDE","CAP_FOWNER") if c not in caps)
    template["linux"].setdefault("resources",{})
    for quota in quotas: capture(quota)
    for roundidx in range(len(cycles)):
        for idx in range(len(quotas)):
            quota=quotas[(idx+roundidx)%len(quotas)]
            conditions=("warm","cold") if (idx+roundidx)%2==0 else ("cold","warm")
            for condition in conditions:
                for mode in cycles[(roundidx+(3 if condition=="cold" else 0)+quotas.index(quota))%len(cycles)]:
                    case(roundidx,quota,condition,mode)
    assert len(samples)==216 and all(r.get("result")=="PASS" for r in samples)
    for quota in quotas:
        assert all(digest(work/("capture-"+quota)/name)==v["sha256"] for name,v in captures[quota]["files"].items())


summary=dict(source=source,base=base,worker_cpus=worker_cpus,expected=216,
             expected_http=3672,clock_start=now(),completed=0,result="FAIL")
try:
    main()
    summary["result"]="PASS"
except BaseException as error:
    summary["error"]=repr(error)
    raise
finally:
    summary["execution_finished_at"]=samples[-1]["finished_at"] if samples else now()
    summary["completed"]=sum(r.get("result")=="PASS" for r in samples)
    summary["observed_http"]=sum(r.get("http_responses",0) for r in samples)
    summary["captures"]=captures
    if (release/"runsc").exists():
        try:
            delete()
        except Exception as cleanup_error:
            summary["cleanup_error"]=repr(cleanup_error)
    (out/"quota-samples.json").write_text(json.dumps(samples,indent=2))
    summary["verdict_at"]=now()
    (out/"quota-summary.json").write_text(json.dumps(summary,indent=2))
    print(json.dumps({k:v for k,v in summary.items() if k!="captures"}), flush=True)
    shutil.rmtree(work,ignore_errors=True)
