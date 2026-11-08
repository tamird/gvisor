"""Measure index storage on one native tmpfs checkpoint with the paired release binaries."""
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
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

TMP = Path(os.environ["RUNNER_TEMP"]) / "graph-size"
OUT = TMP / "results"
BASE = "975fb172241c7946a9f67b4f52e0349d9240589a"
FIX = "aed67b42bf0fb4e1f14cd7bfddaecd5f2e1d9a1d"
PAIR = TMP.parent / "state-working-set"
BENCH = "//test/benchmarks/checkpointfile:checkpointfile_test"
APP = "//test/cmd/test_app:test_app"
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

def build():
    select(BASE)
    build_report = json.loads((PAIR / "results/build.json").read_text())
    assert build_report["verified"] and build_report["arms"]["stock"]["head"] == BASE
    assert build_report["arms"]["combined"]["head"] == FIX
    INSTALL.symlink_to(PAIR / "stock", target_is_directory=True)
    bazel("stock-build", "build", APP, BENCH)
    release = {str(p.relative_to(INSTALL)): digest(p) for p in INSTALL.rglob("*") if p.is_file()}
    assert set(release) == {"runsc", "containerd-shim-runsc-v1", *("gvisor-bin/" + n for n in [
        "checkpointgofer", "gvisor-proc-umounter", "gvisor-sentry-prewarmer", "gvisor_sentry",
        "runsc-fd-parking", "runsc-metric-server", "runsc-netgofer"])}
    assert build_report["arms"]["stock"]["files"] == release
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
          "--test_sharding_strategy=disabled", "--flaky_test_attempts=1", "--test_env=GO_TEST_WRAP_TESTV=1", *owners)
    required = {
        owners[0]: {"TestSliceOfArraysInteriorPointer"},
        owners[1]: {"TestSharedSliceWindows", "TestInteriorPointersIntoSlices",
                    "TestAbsorbedInlineStructSplices",
                    "TestOverlappingArrayPointersAndSlicesGraph/ArrayPtrBeforeOverlappingSlice",
                    "TestOverlappingArrayPointersAndSlicesGraph/SliceBeforeOverlappingArrayPtr"},
    }
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
            files = {}
            for output in result["testActionOutput"]:
                name = output["name"]
                if name not in ("test.xml", "test.log"):
                    continue
                uri = urlsplit(output["uri"])
                assert uri.scheme == "file" and not uri.netloc
                raw = Path(unquote(uri.path)).read_bytes()
                label = owner.split(":")[-1]
                (OUT / (label + "-" + name)).write_bytes(raw)
                files[name] = hashlib.sha256(raw).hexdigest()
                if name == "test.xml":
                    root = ET.fromstring(raw)
                    cases = root.findall(".//testcase")
                    assert not any(c.find("error") is not None or c.find("failure") is not None for c in cases)
                    passed = {c.get("name") for c in cases if c.find("skipped") is None}
                    assert required[owner] <= passed, (owner, required[owner] - passed)
            assert set(files) == {"test.log", "test.xml"}
            tests[owner] = {"config": key["configuration"]["id"], "strategy": info["strategy"],
                            "status": result["status"], "files": files, "required": sorted(required[owner])}
    assert set(tests) == set(owners)
    REPORT["exact_candidate_tests"] = tests
    save()
    (TMP / "benchstat").symlink_to(PAIR / "benchstat")
    assert digest(TMP / "benchstat") == build_report["benchstat_sha256"]
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
