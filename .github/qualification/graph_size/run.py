"""Compare sparse zero-sized targets on a restored tmpfs kernel and two pointer graphs."""
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
from urllib.parse import unquote, urlparse

TMP = Path(os.environ["RUNNER_TEMP"]) / "graph-size"
OUT = TMP / "results"
BASE = "7d1b049f5d541992b47cc38cf564025908a9ed7b"
FIX = "28941a7dd43d1103db628a698841953663f9fdc1"
FILE = Path("test/benchmarks/checkpointfile/checkpointfile.go")
SHAPES = Path("pkg/state/tests/struct_test.go")
LAYOUT = Path("pkg/state/graph_buf_test.go")
PKG = Path(".github/qualification/graph_size")
PAIR = "//.github/qualification/graph_size:paired"
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

def results(bep, owners):
    rows = {}
    with bep.open() as stream:
        for event in map(json.loads, stream):
            if "testResult" not in event:
                continue
            ident = event["id"]["testResult"]
            label = ident["label"].removeprefix("@@")
            result = event["testResult"]
            info = result.get("executionInfo", {})
            assert label not in rows and result["status"] == "PASSED", (label, result["status"])
            assert not result.get("cachedLocally") and not info.get("cachedRemotely")
            assert info["strategy"] in ("local", "standalone"), info["strategy"]
            files = [f for f in result["testActionOutput"] if f["name"] == "test.log"]
            assert len(files) == 1
            uri = urlparse(files[0]["uri"])
            assert uri.scheme == "file" and uri.netloc in ("", "localhost")
            log = Path(unquote(uri.path))
            sha = digest(log)
            assert files[0].get("digest", sha) == sha
            rows[label] = ({"config": ident["configuration"]["id"], "status": result["status"],
                            "strategy": info["strategy"], "log_sha256": sha}, log.read_text())
    assert set(rows) == set(owners), set(rows)
    return rows

def runtests(name, owners, *flags):
    bep = TMP / (name + ".bep")
    bazel(name, "test", "--build_event_json_file=" + str(bep), "--nocache_test_results",
          "--test_sharding_strategy=disabled", "--flaky_test_attempts=1", "--strategy=TestRunner=local",
          "--test_env=GO_TEST_WRAP=0", *flags, *owners)
    return results(bep, owners)

def build():
    select(BASE)
    bazel("stock-release", "build", "//:release", APP)
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
    shape = (TMP / "targets.go.inc").read_bytes()
    ownership = (TMP / "ownership.go.inc").read_bytes()
    internal, external = "//pkg/state:state_test", "//pkg/state/tests:tests_test"
    for arm, source in [("stock", BASE), ("fixed", FIX)]:
        select(source)
        base, layout, shapes = FILE.read_bytes(), LAYOUT.read_bytes(), SHAPES.read_bytes()
        try:
            log = b'\tt.Logf("State file sizes:'
            assert base.count(log) == 1
            write = b'''	if out := os.Getenv("QUALIFICATION_GRAPH_OUTPUT"); out != "" {
		if err := os.WriteFile(out, data, 0600); err != nil {
			t.Fatal(err)
		}
	}
'''
            FILE.write_bytes(base.replace(log, write + log))
            assert shapes.count(b'"bytes"') == layout.count(b'"reflect"') == 1
            SHAPES.write_bytes(shapes.replace(b'"bytes"', b'"bytes"\n\t"crypto/sha256"') + shape)
            bind = (b"id := es.zeroTargets[0]\n\tentry := es.target(id)" if arm == "stock"
                    else b"entry := &es.zeroTargets[0]\n\tid := entry.id")
            assert ownership.count(b'\n\tBIND_TARGET\n') == 1
            LAYOUT.write_bytes(layout.replace(b'"reflect"', b'"reflect"\n\t"runtime"') +
                               ownership.replace(b'\n\tBIND_TARGET\n', b'\n\t' + bind + b'\n'))
            bazel(arm + "-bench", "build", BENCH)
            stage(BENCH, TMP / arm)
            entries = runtests(arm + "-suites", [internal, external], "--test_arg=-test.v")
            required = {internal: ["TestQualificationTargetOwnership"], external: ["TestZeroSizedCrossEngineRoundTrip",
                        "TestZeroSizedFieldPointers", "TestZeroSizedStandaloneWithHooks", "TestRandomGraphs"]}
            for owner, cases in required.items():
                for case in cases:
                    assert re.search(r"(?m)^--- PASS: " + case + r" ", entries[owner][1]), (owner, case)
            stage(external, TMP / (arm + "-shapes"))
            sizes = re.findall(r"TARGET_LAYOUT (\d+) (\d+)", entries[internal][1])
            assert sizes == [("48", "4") if arm == "stock" else ("24", "16")], sizes
            REPORT.setdefault("arms", {})[arm] = {
                "binary": digest(TMP / arm), "shapes_binary": digest(TMP / (arm + "-shapes")),
                "fixture": digest(FILE), "shapes": digest(SHAPES), "layout": [int(n) for n in sizes[0]],
                "suites": {owner: record for owner, (record, _) in entries.items()}}
            save()
        finally:
            FILE.write_bytes(base)
            LAYOUT.write_bytes(layout)
            SHAPES.write_bytes(shapes)
    stock, fixed = REPORT["arms"]["stock"], REPORT["arms"]["fixed"]
    assert stock["binary"] != fixed["binary"] and stock["fixture"] == fixed["fixture"]
    assert stock["shapes"] == fixed["shapes"] and stock["shapes_binary"] != fixed["shapes_binary"]
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
    REPORT["built_tools"] = {m:digest(TMP/m) for m in ("stock","fixed","benchstat")}
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

def paired():
    pristine = media()
    assert json.loads((OUT / "guest.json").read_text())["media"] == pristine
    paths = dict(zip(("stock", "fixed"), map(Path, sys.argv[2:4]), strict=True))
    shape_paths = dict(zip(("stock", "fixed"), map(Path, sys.argv[4:]), strict=True))
    built = json.loads((OUT / "build.json").read_text())["arms"]
    for name, path in paths.items():
        assert digest(path) == built[name]["binary"] and digest(shape_paths[name]) == built[name]["shapes_binary"]
    cpu = min(os.sched_getaffinity(0))
    env = {**os.environ, "GOMAXPROCS": "1", "GO_TEST_WRAP": "0"}
    def child(name, arm, directory, *opts, extra=None):
        return run(name, ["taskset", "-c", str(cpu), paths[arm], "-checkpoint-dir=" + str(directory),
                          "-test.cpu=1", *opts], env={**env, **(extra or {})}, timeout=180).read_text()
    def roundtrip(arm, source, dest=None):
        extra = {"QUALIFICATION_GRAPH_OUTPUT": str(dest)} if dest else None
        text = child("check-" + arm + "-" + source.name, arm, source,
                     "-test.run=^TestCheckpointFileRoundTrip$", "-test.v", extra=extra)
        assert re.search(r"(?m)^--- PASS: TestCheckpointFileRoundTrip ", text) and "--- SKIP:" not in text
        sizes = re.findall(r"State file sizes: original (\d+), re-encoded \[(\d+) (\d+) (\d+)\]", text)
        assert len(sizes) == 1
        nums = [int(n) for n in sizes[0]]
        assert nums[0] == (source / "checkpoint.img").stat().st_size and min(nums) > 0
        if dest:
            assert dest.stat().st_size == nums[-1]
        return {"sizes": nums, "input": digest(source / "checkpoint.img")}
    for arm in paths:
        d = TMP / (arm + "-media")
        d.mkdir()
        for name in NAMES[1:]:
            (d / name).symlink_to(MEDIA / name)
        REPORT[arm + "_roundtrip"] = roundtrip(arm, MEDIA, d / "checkpoint.img")
    for arm, other in [("stock", "fixed"), ("fixed", "stock")]:
        REPORT[arm + "_reads_" + other] = roundtrip(arm, TMP / (other + "-media"))
    REPORT["cpu"] = cpu
    save()
    cases = {"CheckpointFile/Encode": 1, "CheckpointFile/Decode": 1,
             "QualificationTargets/Ordinary": 8, "QualificationTargets/ZeroSized": 8}
    witnesses = {}
    rows = {arm: [] for arm in paths}
    order = []
    for pair in range(12):
        for arm in (("stock", "fixed") if pair % 2 == 0 else ("fixed", "stock")):
            for case, n in cases.items():
                owner, sub = case.split("/")
                flags = ("-test.run=^$", f"-test.bench=^Benchmark{owner}$/^{sub}$",
                         "-test.benchmem", f"-test.benchtime={n}x")
                name = f"{pair}-{arm}-{sub}"
                if owner == "CheckpointFile":
                    text = child(name, arm, MEDIA, *flags)
                    unit = "encoded-kernel-B" if sub == "Encode" else "checkpoint-total-B"
                else:
                    text = run(name, ["taskset", "-c", str(cpu), shape_paths[arm], "-test.cpu=1",
                                      "-test.v", *flags], env=env, timeout=180).read_text()
                    unit = "parents/op"
                result = re.findall(r"(?m)^Benchmark" + case + r"(?:-1)?\s+" + str(n) + r"\s+[^\n]+", text)
                assert len(result) == 1 and re.search(r"(?m)^PASS$", text) and "SKIP" not in text, text[-700:]
                assert all(metric in result[0] for metric in ("ns/op", "B/op", "allocs/op", unit))
                if owner == "QualificationTargets":
                    observed = set(re.findall(r"TARGET_GRAPH " + sub + r" (\d+) ([0-9a-f]{64})", text))
                    assert len(observed) == 1 and re.search(r"\b4096 parents/op\b", result[0])
                    witness, = observed
                    assert witnesses.setdefault(sub, witness) == witness
                rows[arm].append(result[0])
            order.append((pair, arm))
            (OUT / (arm + ".bench")).write_text("\n".join(rows[arm]) + "\n")
    assert media() == pristine
    REPORT.update(order=order, media=pristine, deterministic_shapes=witnesses, verified=True)

def measure():
    head = os.environ["CARRIER"]
    select(head)
    for arm in ("stock", "fixed", "stock-shapes", "fixed-shapes"):
        shutil.copy2(TMP / arm, PKG / (arm + ".bin"))
    entries = runtests("paired-run", [PAIR], "--test_timeout=1800", "--test_env=RUNNER_TEMP=" + str(TMP.parent))
    REPORT["paired_owner"] = entries[PAIR][0]
    assert json.loads((OUT / "paired.json").read_text())["verified"]
    p = subprocess.run([TMP / "benchstat", "stock=stock.bench", "sparse=fixed.bench"], cwd=OUT, capture_output=True, text=True, check=True)
    (OUT / "benchstat.txt").write_text(p.stdout)
    assert all(name in p.stdout for name in ("CheckpointFile/Encode", "CheckpointFile/Decode", "QualificationTargets/Ordinary", "QualificationTargets/ZeroSized"))
    REPORT["verified"] = True

try:
    {"build":build,"guest":guest,"measure":measure,"paired":paired}[STAGE]()
except Exception:
    REPORT["failure"] = traceback.format_exc(limit=5)[-2400:]
    raise
finally:
    save()
