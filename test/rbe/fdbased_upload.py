"""Run the existing four-stream iperf3 upload on the two #16887 releases."""
from contextlib import ExitStack
import datetime
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse

from image_archive import archive
from image_runtime import prepared_runtime

ROOT = Path(sys.argv[1])
OUT = ROOT / "public"
SOURCE = {"before": "471d6dff5229b99208bf6fec985601067f1f9d69", "after": "430e65796267c361c86d13b66b4a4604975d0e13"}
OWNER = "//test/benchmarks/network:iperf_test"
CASE = "BenchmarkIperfManyConnections/operation.Upload/parallel.4"
BRIDGE = "fdbased-upload"
PAIRS = 12
DEADLINE = time.monotonic() + 56 * 60
EXEC = ["docker", "exec", "fdbased-builder"]
BZ = EXEC + ["bazel", "--bazelrc=" + str(ROOT / "remote.bazelrc")]
OPTIONS = ["--config=x86_64", "--compilation_mode=opt", "--noannounce_rc"]
R = {"status": "INCOMPLETE", "sources": SOURCE, "samples": [], "builds": {}, "witness": {}, "commands": [],
     "expected_measured": 2 * PAIRS, "expected_warmups": 2, "fixture": str(CASE), "deadline_seconds": 56 * 60}


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def persist() -> None:
    (OUT / "result.json").write_text(json.dumps(R, indent=2) + "\n")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(label: str, argv: list[str], limit: int = 600) -> bytes:
    R["phase"] = label
    persist()
    cmd = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         timeout=max(1, min(limit, DEADLINE - time.monotonic())))
    if cmd.returncode:
        private = cmd.stdout + cmd.stderr
        terms = ("undefined:", "strict dependencies", "permission denied", "no such file or directory",
                 "failed to fetch", "no matching", "failed to start", "no space left", "timed out",
                 "unknown flag", "no such target", "cannot connect", "error loading")
        text = private.decode(errors="replace").lower()
        R["commands"].append({"phase": label, "exit": cmd.returncode, "diagnostic_sha256": digest(private),
                              "signatures": [t for t in terms if t in text]})
        raise RuntimeError(label)
    return cmd.stdout


def checkout(source: str) -> None:
    run("select-source", ["git", "checkout", "-q", "--detach", source])
    assert run("verify-source", ["git", "rev-parse", "HEAD"]).decode().strip() == source


def build(key: str, source: str, target: str) -> dict:
    checkout(source)
    bep = ROOT / (key + ".build.bep")
    argv = BZ + ["build", *OPTIONS, "--build_event_json_file=" + str(bep), target]
    run("build-" + key, argv, 1200)
    events = [json.loads(line) for line in bep.read_text().splitlines()]
    owners = [e["id"]["targetCompleted"]["label"] for e in events if e.get("completed", {}).get("success") and "targetCompleted" in e["id"]]
    assert (target in owners if not target.startswith("@") else any(n.endswith("//cmd/benchstat:benchstat") for n in owners)), key
    paths = run("configured-" + key, BZ + ["cquery", *OPTIONS, "--output=files", target]).decode().splitlines()
    destination = ROOT / key
    destination.mkdir()
    items = {}
    for name in paths:
        path = Path(name)
        assert not path.is_absolute() and path.parts[0] == "bazel-out" and ".." not in path.parts, key
        rel = str(path).split("/bin/release/", 1)[1] if target == "//:release" else path.name
        assert rel not in items
        copy = destination / rel
        copy.parent.mkdir(parents=True, exist_ok=True)
        run("stage-" + key, EXEC + ["cp", "-L", name, str(copy)])
        actual = digest(copy.read_bytes())
        assert run("hash-" + key, EXEC + ["sha256sum", name]).decode().split()[0] == actual
        items[rel] = {"bytes": copy.stat().st_size, "sha256": actual}
    assert items
    R["builds"][key] = {"source": source, "target": target, "files": items, "bep_sha256": digest(bep.read_bytes())}
    persist()
    return items


def witness(side: str, runtime: str, process: subprocess.Popen) -> None:
    # Count one syscall on existing threads of this fixture's exact Sentry. No
    # follow-forks, argument decoding, packet capture or host-wide tracing.
    deadline = min(DEADLINE, time.monotonic() + 150)
    while process.poll() is None and time.monotonic() < deadline:
        ids = subprocess.check_output(["docker", "ps", "-q", "--filter", "network=" + BRIDGE]).decode().split()
        for cid in ids:
            result = subprocess.run(["docker", "inspect", "-f", "{{.HostConfig.Runtime}} {{.State.Pid}}", cid],
                                    capture_output=True, text=True, timeout=5)
            values = result.stdout.split()
            if not result.returncode and len(values) == 2 and values[0] == runtime and int(values[1]) > 0:
                pid = int(values[1])
                tasks = Path("/proc") / str(pid)
                image = digest((tasks / "exe").read_bytes())
                assert image == R["builds"][side]["files"]["gvisor-bin/gvisor_sentry"]["sha256"], "Sentry executable identity"
                time.sleep(1.5)
                inode = (tasks / "exe").stat().st_ino
                tids = sorted(int(p.name) for p in (tasks / "task").iterdir())
                assert tids and tasks.joinpath("exe").stat().st_ino == inode
                summary = ROOT / (side + ".syscalls")
                with (ROOT / (side + ".trace-status")).open("wb") as stderr:
                    args = ["strace", "-c", "-e", "trace=sendmmsg", "-e", "status=successful", "-o", str(summary)]
                    for tid in tids:
                        args.extend(["-p", str(tid)])
                    trace = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr)
                    try:
                        time.sleep(3)
                    finally:
                        if trace.poll() is None:
                            trace.send_signal(signal.SIGINT)
                        rc = trace.wait(timeout=10)
                raw = summary.read_bytes() if summary.exists() else b""
                lines = [line.split() for line in raw.decode().splitlines() if line.split()[-1:] == ["sendmmsg"]]
                counts = int(lines[0][3]) if len(lines) == 1 else 0
                R["witness"][side] = {"trace_exit": rc, "successful_sendmmsg": counts, "threads": len(tids),
                                      "sentry_sha256": image, "summary_sha256": digest(raw)}
                persist()
                assert rc in (0, -signal.SIGINT) and counts > 0, "no successful Sentry sendmmsg"
                return
        time.sleep(.12)
    raise RuntimeError("owned Sentry did not appear during warmup")


def observe(side: str, pair: int, runtime: str, subnet: ipaddress.IPv4Network) -> None:
    name = f"{side}-{pair}"
    R["phase"] = name
    persist()
    bep = ROOT / (name + ".bep")
    args = BZ + ["test", *OPTIONS, "--strategy=TestRunner=local", "--nocache_test_results",
                 "--runs_per_test=1", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled",
                 "--nozip_undeclared_test_outputs", "--test_timeout=150", "--test_env=GO_TEST_WRAP=1",
                 "--test_env=GO_TEST_WRAP_TESTV=0", "--test_arg=-test.run=^$",
                 "--test_arg=-test.bench=^BenchmarkIperfManyConnections$/^operation.Upload$/^parallel.4$",
                 "--test_arg=-test.benchtime=1x", "--test_arg=-test.count=1", "--test_arg=-runtime=" + runtime,
                 "--build_event_json_file=" + str(bep), "--build_event_json_file_path_conversion=false", OWNER]
    witness_error = None
    with (ROOT / (name + ".log")).open("wb") as output:
        with subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT) as proc:
            try:
                if pair == -1:
                    witness(side, runtime, proc)
            except Exception as exc:
                witness_error = exc
            finally:
                rc = proc.wait(timeout=max(1, min(210, DEADLINE - time.monotonic())))
    R["execution_finished_at"] = now()
    record = {"side": side, "pair": pair, "source": SOURCE[side], "bazel_exit": rc, "verified": False}
    if witness_error is not None:
        record["witness_error"] = type(witness_error).__name__
    R["samples"].append(record)
    events = [json.loads(line) for line in bep.read_text().splitlines()]
    tests = [e for e in events if "testResult" in e]
    record["test_results"] = len(tests)
    persist()
    test, = tests
    assert test["id"]["testResult"]["label"] == OWNER
    result = test["testResult"]
    record["native_status"] = result["status"]
    record["strategy"] = result.get("executionInfo", {}).get("strategy")
    assert not result.get("cachedLocally") and not result.get("executionInfo", {}).get("cachedRemotely")
    outputs = result["testActionOutput"]
    def read(item: dict) -> bytes:
        uri = urlparse(item["uri"])
        assert uri.scheme == "file" and not uri.netloc and "/testlogs/test/benchmarks/network/iperf_test/" in uri.path
        return subprocess.check_output(EXEC + ["cat", unquote(uri.path)])
    log = read(next(f for f in outputs if f["name"] == "test.log"))
    record["testlog_sha256"] = digest(log)
    record["signatures"] = [term for term in ("failed to start", "failed to wait", "private receiver IP",
                            "failed to run client", "permission denied", "no such file", "cannot connect") if term.lower() in log.decode(errors="replace").lower()]
    persist()
    if witness_error is not None:
        raise witness_error
    assert rc == 0 and result["status"] == "PASSED" and record["strategy"] in ("local", "standalone")
    obs, = [f for f in outputs if f["name"].startswith("test.outputs/iperf-") and f["name"].endswith(".json")]
    raw = read(obs)
    o = json.loads(raw)
    assert o["Name"] == CASE and (o["Iterations"],o["Parallel"],o["DurationSeconds"],o["OmitSeconds"]) == (1,4,10,2)
    data = o["Output"]
    s, end = data["start"]["test_start"], data["end"]
    assert not data.get("error") and (s["protocol"], s["num_streams"], s["duration"], s["omit"], s["blksize"]) == ("TCP",4,10,2,131072)
    connected = data["start"]["connected"]
    assert s["bytes"] == 0 and s["blocks"] == 0
    assert len(connected)==4 and len({c["socket"] for c in connected})==4
    for conn in connected:
        addr = [ipaddress.ip_address(conn[k]) for k in ("local_host","remote_host")]
        assert addr[0]!=addr[1] and all(a in subnet and not a.is_loopback for a in addr)
    streams = end["streams"]
    assert len(streams)==4 and all(v["sender"]["bytes"]>0 and v["receiver"]["bytes"]>0 for v in streams)
    sent, received = end["sum_sent"], end["sum_received"]
    assert sent["bytes"]==sum(v["sender"]["bytes"] for v in streams) and received["bytes"]==sum(v["receiver"]["bytes"] for v in streams)
    assert 9.5 <= sent["seconds"] <= 12 and math.isfinite(sent["bits_per_second"]) and sent["bits_per_second"]>0
    assert 0.90 <= received["bytes"]/sent["bytes"] <= 1.10
    bench, = [line for line in log.decode().splitlines() if re.match(re.escape(CASE)+r"-\d+\s+1\s+",line)]
    items=bench.split()
    metrics={k:float(v) for v,k in zip(items[2::2],items[3::2])}
    rate=metrics["bandwidth.bytes_per_second"]
    assert math.isclose(rate, sent["bits_per_second"]/8, rel_tol=1e-5)
    record.update(verified=True, measured_seconds=sent["seconds"], sent=sent["bytes"], received=received["bytes"],
                  bytes_per_second=rate, iperf_json_sha256=digest(raw), streams=4, verified_at=now())
    persist()


def main() -> None:
    os.environ.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0=os.getcwd(),
                      BUILD_WORKSPACE_DIRECTORY=os.getcwd(), APPLICABILITY_WORK_DEADLINE=str(DEADLINE))
    R["started_at"] = now()
    R["carrier"] = run("carrier",["git","rev-parse","HEAD"]).decode().strip()
    R["checker_sha256"] = digest(Path(__file__).read_bytes())
    R["host_arch"] = os.uname().machine
    R["source_changed_files"] = run("compare", ["git", "diff", "--name-only", SOURCE["before"], SOURCE["after"]]).decode().splitlines()
    assert set(R["source_changed_files"]) == {"pkg/tcpip/link/fdbased/" + name for name in ("BUILD","endpoint.go","endpoint_test.go")}
    R["fixture_sha256"] = {name: digest((ROOT / "fixture" / name).read_bytes()) for name in ("iperf_test.go","iperf.go")}
    names = {"runsc","containerd-shim-runsc-v1"} | {"gvisor-bin/" + n for n in
             ("gvisor_sentry","checkpointgofer","runsc-metric-server","runsc-fd-parking","gvisor-sentry-prewarmer","gvisor-proc-umounter")}
    for side, source in SOURCE.items():
        files = build(side, source, "//:release")
        assert files.keys() == names
        for name in files:
            data = (ROOT / side / name).read_bytes()
            assert data[:4] == b"\x7fELF" and int.from_bytes(data[18:20],"little") == 62
        endpoint = run("endpoint-source", ["git","show",source + ":pkg/tcpip/link/fdbased/endpoint.go"])
        assert b"rawfile.NonBlockingSendMMsg(" in endpoint and b"sendBatch(" in endpoint
        R["builds"][side]["endpoint_sha256"] = digest(endpoint)
    assert R["builds"]["before"]["files"]["gvisor-bin/gvisor_sentry"]["sha256"] != R["builds"]["after"]["files"]["gvisor-bin/gvisor_sentry"]["sha256"]
    image, = build("image","e61da06dc253bd3a62c6583f3350520d305d07c5","//images/benchmarks/iperf:iperf_archive")
    stat, = build("benchstat","2b51abee8530385431b7225198fdd5cd65e78696","@org_golang_x_perf//cmd/benchstat")
    checkout(SOURCE["before"])
    for name,destination in (("iperf_test.go","network"),("iperf.go","tools")):
        shutil.copyfile(ROOT/"fixture"/name, Path("test/benchmarks")/destination/name)
    run("common-fixture-build", BZ+["build", *OPTIONS, OWNER], 900)
    blob = ROOT / "image" / image
    image_record = {}
    config, _, _ = archive(blob, image_record)
    assert (config["architecture"],config["os"]) == ("amd64","linux")
    run("load-image", ["docker","load","-i",str(blob)])
    expected_image = "sha256:" + image_record["config_sha256"]
    tag = "gvisor.dev/images/benchmarks/iperf:latest"
    assert run("image-config", ["docker","image","inspect","-f","{{.Id}}",tag]).decode().strip() == expected_image
    R["iperf_image_config"] = expected_image
    with ExitStack() as scope:
        run("create-network",["docker","network","create","--internal","--driver=bridge",BRIDGE])
        scope.callback(subprocess.run,["docker","network","rm",BRIDGE], check=True, capture_output=True, timeout=30)
        network,=json.loads(run("inspect-network",["docker","network","inspect",BRIDGE]))
        subnet=ipaddress.ip_network(network["IPAM"]["Config"][0]["Subnet"])
        assert network["Internal"] and network["Driver"]=="bridge" and subnet.is_private
        R["network"] = {"driver":"bridge","internal":True,"platform":"systrap","network":"sandbox"}
        daemon_config=Path("/etc/docker/daemon.json")
        if not daemon_config.exists():
            daemon_config.write_text("{}\n")
            scope.callback(daemon_config.unlink)
        runtimes = {}
        for side,source in SOURCE.items():
            runtime="fdbased-"+side
            runtimes[side]=runtime
            metadata=ROOT/("metadata-"+side)
            metadata.mkdir()
            opts={"runtime_name":runtime,"image_name":"benchmarks/iperf","lifecycle_only":True,
                  "retained_release_files":R["builds"][side]["files"],"release_supplier":source,
                  "runtime_flags":["--platform=systrap","--network=sandbox"],"cleanup_reserve_seconds":60}
            R["phase"]="install-"+side
            persist()
            scope.enter_context(prepared_runtime(metadata/"runtime.json",ROOT/side,opts))
        for side in SOURCE:
            observe(side,-1,runtimes[side],subnet)
        for pair in range(PAIRS):
            for side in (list(SOURCE) if pair%2==0 else list(reversed(SOURCE))):
                observe(side,pair,runtimes[side],subnet)
    for side in SOURCE:
        rows=[r for r in R["samples"] if r["side"]==side and r["pair"]>=0 and r["verified"]]
        assert [r["pair"] for r in rows]==list(range(PAIRS))
        (OUT/(side+".bench")).write_text("".join(f"{CASE}  1  {r['bytes_per_second']:.9g} B/s\n" for r in rows))
    result=run("benchstat",[str(ROOT/"benchstat"/stat),str(OUT/"before.bench"),str(OUT/"after.bench")])
    result=result.replace(str(OUT).encode()+b"/",b"")
    assert b"operation.Upload/parallel.4" in result and b"n=12" in result
    (OUT/"benchstat.txt").write_bytes(result)
    R.update(status="PASS",phase="verified")


try:
    main()
except (Exception, KeyboardInterrupt) as exc:
    R["status"]="FAIL"
    # Only source-selected diagnostics and local source locations are public.
    import traceback
    R["exception_type"]=type(exc).__name__
    R["source_frames"]=[f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename).name in {"fdbased_upload.py","image_runtime.py","image_archive.py"}]
    if isinstance(exc,AssertionError):
        R["assertion"] = str(exc) if str(exc) in {"no successful Sentry sendmmsg","Sentry executable identity"} else "source postcondition"
    raise
finally:
    verified=[r for r in R["samples"] if r["verified"]]
    R["executed"] = len(R["samples"])
    R["failed"] = [r["side"]+"-"+str(r["pair"]) for r in R["samples"] if not r["verified"]]
    R["verified_measured"]=len([r for r in verified if r["pair"]>=0])
    R["verified_warmups"]=len([r for r in verified if r["pair"]<0])
    R["remaining_measured"]=R["expected_measured"]-R["verified_measured"]
    R["verified_at"]=now()
    persist()
