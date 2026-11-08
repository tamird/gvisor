"""Measure the graph encoder's final buffer on one native tmpfs checkpoint."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import traceback

TMP = Path(os.environ["RUNNER_TEMP"]) / "graph-size"
OUT = TMP / "results"
BASE = "7d1b049f5d541992b47cc38cf564025908a9ed7b"
FIX = "ccb9983cc6c9ddf727b4c9c71d5bbec98fad408b"
FILE = Path("pkg/state/graph_encode_stream_unsafe.go")
BENCH = "//test/benchmarks/checkpointfile:checkpointfile_test"
APP = "//test/cmd/test_app:test_app"
STATS = "@org_golang_x_perf//cmd/benchstat:benchstat"
BAZEL = [str(TMP / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g"]
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off", "--remote_executor=",
         "--remote_cache=", "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
         "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox"]
INSTALL, BUNDLE, MEDIA = (TMP / x for x in ["release", "bundle", "media"])
NAMES = ["checkpoint.img", "pages_meta.img", "pages.img", "spec.json"]
REPORT = {"baseline": BASE, "candidate": FIX}
STAGE = sys.argv[1]

def save():
    (OUT / (STAGE + ".json")).write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for data in iter(lambda: f.read(1 << 20), b""):
            h.update(data)
    return h.hexdigest()

def run(name, args, *, env=None, timeout=2400):
    log = TMP / (name + ".log")
    with log.open("wb") as output:
        p = subprocess.run(args, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
    if p.returncode:
        lines = log.read_text(errors="replace").splitlines()
        errs = [s[:350] for s in lines if re.search(r"(?i)(^FAIL|fatal|error|failed|permission|invalid|not supported|not found)", s)]
        raise RuntimeError((name, p.returncode, errs[:5] + [s[:350] for s in lines[-3:]]))
    return log

def bazel(name, command, *args):
    return run(name, [*BAZEL, command, *FLAGS, *args])

def stage(label, dest):
    log = bazel("path-" + dest.name, "cquery", "--output=files", label)
    files = [Path(s) for s in log.read_text().splitlines() if s.startswith("bazel-out/")]
    files = [f for f in files if f.is_file() and os.access(f, os.X_OK)]
    assert len(files) == 1, (label, files)
    shutil.copy2(files[0], dest)
    with dest.open("rb") as f:
        h = f.read(20)
    assert h[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", h, 18)[0] == 62

def select(source):
    subprocess.run(["git", "checkout", "-q", "--detach", source], check=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
    subprocess.run(["git", "diff", "--exit-code", "--quiet", "HEAD"], check=True)

def instrument():
    s = FILE.read_text()
    old = 'w.grow(streamHeaderMaxBytes + len(es.types.buf.b) + len(tables.b) + dataMaxBytes)'
    new = '''request := streamHeaderMaxBytes + len(es.types.buf.b) + len(tables.b) + dataMaxBytes
	fixedRequest := request
	mode := os.Getenv("GRAPH_SIZE_CENSUS")
	exact := 0
	kernel := ti.typ.Name() == "Kernel" && ti.typ.PkgPath() == "gvisor.dev/gvisor/pkg/sentry/kernel"
	if kernel {
		for i := int32(0); i < int32(es.objs.n); i++ {
			o := es.obj(i)
			if o.parent < 0 && !o.dead && !o.isMap && !o.isArray && (i == 0 || !es.types.isInexact(o.ti)) {
				exact++
			}
		}
		if mode == "stock" {
			request += binary.MaxVarintLen64 * exact
		}
	}
	before := cap(w.b)
	w.grow(request)
	after := cap(w.b)'''
    line = '''if kernel {
		fmt.Fprintf(os.Stderr, "GRAPH_SIZE %s objects=%d targets=%d exact=%d frame=%d request=%d fixedRequest=%d stockRequest=%d before=%d after=%d\\n", mode, n, es.targets.n, exact, len(w.b), request, fixedRequest, fixedRequest+binary.MaxVarintLen64*exact, before, after)
	}
	'''
    for a,b in [('"io"', '"io"\n\t"fmt"\n\t"os"'), (old,new), ("es.stats.objects = int(n)",line+"es.stats.objects = int(n)")]:
        assert s.count(a) == 1, a
        s=s.replace(a,b)
    FILE.write_text(s)

def build():
    select(BASE)
    bazel("stock-build", "build", "//:release", APP, BENCH)
    shutil.copytree("bazel-bin/release", INSTALL)
    release = {str(p.relative_to(INSTALL)): digest(p) for p in INSTALL.rglob("*") if p.is_file()}
    assert set(release) == {"runsc", "containerd-shim-runsc-v1", *("gvisor-bin/" + n for n in [
        "checkpointgofer", "gvisor-proc-umounter", "gvisor-sentry-prewarmer", "gvisor_sentry",
        "runsc-fd-parking", "runsc-metric-server", "runsc-netgofer"])}
    REPORT["stock_release"] = release
    rootfs = BUNDLE / "rootfs"
    rootfs.mkdir(parents=True)
    stage(APP, rootfs / "test_app")
    REPORT["test_app"] = digest(rootfs / "test_app")
    stage(BENCH, TMP / "stock")
    select(FIX)
    bazel("fixed-build", "build", BENCH)
    stage(BENCH, TMP / "fixed")
    bep = TMP / "state.bep"
    owners = ["//pkg/state:state_test", "//pkg/state/tests:tests_test"]
    bazel("state-tests", "test", "--build_event_json_file="+str(bep), "--nocache_test_results",
          "--test_sharding_strategy=disabled", "--flaky_test_attempts=1", *owners)
    tests = {}
    for line in bep.open():
        e = json.loads(line)
        if "testResult" in e:
            key = e["id"]["testResult"]
            result = e["testResult"]
            info = result.get("executionInfo", {})
            owner = key["label"].removeprefix("@@")
            assert owner not in tests and result["status"] == "PASSED" and not result.get("cachedLocally") and not info.get("cachedRemotely")
            assert info.get("strategy") in ("local","linux-sandbox","standalone","processwrapper-sandbox")
            tests[owner] = {"config": key["configuration"]["id"], "strategy": info["strategy"], "status": result["status"]}
    assert set(tests) == set(owners)
    REPORT["exact_candidate_tests"] = tests
    save()
    original = FILE.read_bytes()
    try:
        instrument()
        REPORT["census_source_sha256"] = digest(FILE)
        bazel("census-build", "build", BENCH)
        stage(BENCH, TMP / "census")
    finally:
        FILE.write_bytes(original)
    module = Path("MODULE.bazel")
    clean = module.read_bytes()
    try:
        additions = (TMP / "benchstat.module").read_bytes()
        assert hashlib.sha256(additions).hexdigest() == "373bba0a70f04c2a39a30a45b4acd23f3bfa884e05a5e27cce1ce7010c406cd5"
        module.write_bytes(clean + b"\n" + additions)
        bazel("benchstat-build", "build", STATS)
        stage(STATS, TMP / "benchstat")
    finally:
        module.write_bytes(clean)
    REPORT["built_tools"] = {m:digest(TMP/m) for m in ("stock","fixed","census","benchstat")}
    REPORT["verified"] = True

def media():
    return {n: {"sha256": digest(MEDIA / n), "bytes": (MEDIA / n).stat().st_size} for n in NAMES}

def guest():
    assert os.geteuid() == 0
    MEDIA.mkdir()
    (BUNDLE/"rootfs"/"tmpfs").mkdir()
    spec = dict(ociVersion="1.0.2", root=dict(path="rootfs"), process=dict(terminal=False,
        user=dict(uid=0,gid=0), cwd="/", args=["/test_app","reaper"], env=["PATH=/","GOMAXPROCS=2"]),
        mounts=[dict(type="proc",source="proc",destination="/proc"), dict(type="tmpfs",source="tmpfs",destination="/tmpfs")],
        linux=dict(namespaces=[dict(type=t) for t in ("pid","mount","ipc","uts","network")]))
    (BUNDLE/"config.json").write_text(json.dumps(spec))
    def invoke(name,*args):
        return run(name,[INSTALL/"runsc","--root="+str(TMP/"runtime"),"--platform=systrap","--network=none",
                   "--overlay2=none","--host-settings=ignore","--ignore-cgroups",*map(str,args)],
                   env={**os.environ,"GVISOR_SIDECAR_BINARIES_DIR":str(INSTALL/"gvisor-bin")},timeout=240)
    tree = ["--target-dir=/tmpfs","--depth=50","--file-per-level=1000","--file-size=0"]
    cid = "graph-size"
    try:
        invoke("new","run","--detach","--bundle="+str(BUNDLE),cid)
        invoke("populate","exec",cid,"/test_app","fsTreeCreate",*tree)
        invoke("source-check","exec",cid,"/test_app","fsTreeVerify",*tree)
        invoke("capture","checkpoint","--leave-running","--compression=none","--state-engine=graph","--image-path="+str(MEDIA),cid)
        invoke("drop","delete","--force",cid)
        invoke("restore","restore","--detach","--bundle="+str(BUNDLE),"--image-path="+str(MEDIA),cid)
        invoke("restored-check","exec",cid,"/test_app","fsTreeVerify",*tree)
        (MEDIA/"spec.json").write_text(json.dumps({"names":["__no_name_0"],"mounts":["/","/tmpfs"]}))
        for p in [MEDIA, *(MEDIA/n for n in NAMES)]:
            os.chown(p,TMP.stat().st_uid,TMP.stat().st_gid)
        REPORT.update(verified=True,files=50000,media=media())
    finally:
        invoke("cleanup","delete","--force",cid)

def measure():
    pristine = media()
    assert json.loads((OUT/"guest.json").read_text())["media"] == pristine
    cpu = min(os.sched_getaffinity(0))
    REPORT["cpu"] = cpu
    def child(name, binary, *opts, env=None):
        return run(name, ["taskset","-c",str(cpu),TMP/binary,"-checkpoint-dir="+str(MEDIA),"-test.cpu=1",*opts],
                   env={**os.environ,"GO_TEST_WRAP":"0","GOMAXPROCS":"1",**(env or {})},timeout=180).read_text()
    args = ["-test.run=^$", "-test.bench=^BenchmarkCheckpointFile$/^Encode$", "-test.benchtime=1x", "-test.benchmem"]
    for mode in ("stock","fixed"):
        text = child("roundtrip-"+mode,mode,"-test.run=^TestCheckpointFileRoundTrip$","-test.v")
        assert re.search(r"(?m)^--- PASS: TestCheckpointFileRoundTrip ",text) and not re.search(r"(?m)^--- SKIP:",text)
        REPORT.setdefault("roundtrip",{})[mode] = {"sha256":hashlib.sha256(text.encode()).hexdigest(),
            "log":[s.strip() for s in text.splitlines() if "State file sizes:" in s or "task" in s.lower()][:8]}
        text = child("census-"+mode,"census",*args,env={"GRAPH_SIZE_CENSUS":mode})
        matches = re.findall(r"GRAPH_SIZE "+mode+r" (objects=\d+ targets=\d+ exact=\d+ frame=\d+ request=\d+ fixedRequest=\d+ stockRequest=\d+ before=\d+ after=\d+)",text)
        assert len(matches)==1, (matches,text[-500:])
        fields = {k:int(v) for k,v in re.findall(r"(\w+)=(\d+)",matches[0])}
        assert fields["objects"] >= 50000 and 0 < fields["exact"] <= fields["objects"]
        assert fields["frame"] <= fields["request"] <= fields["after"]
        assert fields["stockRequest"] - fields["fixedRequest"] == 10 * fields["exact"]
        assert fields["request"] == fields[mode+"Request"]
        REPORT.setdefault("census",{})[mode] = fields
        save()
    for mode in ("stock","fixed"):
        child("warmup-"+mode,mode,*args)
    rows = {m:[] for m in ("stock","fixed")}
    for pair in range(12):
        for mode in (("stock","fixed") if pair % 2 == 0 else ("fixed","stock")):
            text = child(f"row-{pair}-{mode}",mode,*args)
            lines = [s for s in text.splitlines() if re.match(r"^BenchmarkCheckpointFile/Encode(?:-1)?\s+1\s",s)]
            assert len(lines)==1 and "ns/op" in lines[0] and "B/op" in lines[0] and "allocs/op" in lines[0], text[-700:]
            rows[mode].append(lines[0])
            REPORT["rows"] = rows
            save()
    assert media() == pristine
    for mode, values in rows.items():
        (OUT/(mode+".txt")).write_text("\n".join(values)+"\n")
    p = subprocess.run([TMP/"benchstat","stock=stock.txt","fixed=fixed.txt"],cwd=OUT,capture_output=True,text=True,check=True)
    (OUT/"benchstat.txt").write_text(p.stdout)
    assert "Encode" in p.stdout and "n=12" in p.stdout
    REPORT.update(verified=True,media=pristine)

try:
    {"build":build,"guest":guest,"measure":measure}[STAGE]()
except Exception:
    REPORT["failure"] = traceback.format_exc(limit=5)[-2400:]
    raise
finally:
    save()
