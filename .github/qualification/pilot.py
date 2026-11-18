"""Bounded BASE-runtime capability, with exact canonical category accounting."""

import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

mode = sys.argv[1]
work, results, support = map(Path, sys.argv[2:5])
active_process = None


def command(*args, timeout=30, check=True):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=check)


def containers():
    return command("sudo", "docker", "ps", "-aq").stdout.split()


def remove_owned():
    record = work / "owned-containers.json"
    owned = set(json.loads(record.read_text())) if record.exists() else set()
    if (work / "active-visit").exists():
        owned.update(containers())
    for identifier in sorted(owned):
        command("sudo", "docker", "rm", "-f", identifier, check=False)
    record.write_text("[]\n")
    (work / "active-visit").unlink(missing_ok=True)


def interrupted(number, frame):
    if active_process is not None and active_process.poll() is None:
        os.killpg(active_process.pid, signal.SIGTERM)
        try:
            active_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(active_process.pid, signal.SIGKILL)
            active_process.wait()
    raise SystemExit(128 + number)


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)
if mode == "cleanup":
    remove_owned()
    subprocess.run(["python3", str(support / "docker_cleanup.py"), str(work), str(results)], check=True, timeout=60)
    raise SystemExit(0)
expected = json.loads((support / "expected.json").read_text())
if mode == "analyze":
    proof = {"runtimeSource": "1f33fe3003455db178b97f3b43d3a012f1611994", "harnessSource": "b5933c08f6a5730dc174dbe3e377c70c01c6ef20", "plan": os.environ["PLAN"], "categories": {}, "statisticalQualification": False}
    for category, leaves in expected.items():
        records = json.loads((results / (category + "-evidence.json")).read_text())
        merged = {}
        for record in records:
            assert record["runtime"] == "systrap"
            assert not (set(merged) & record["samples"].keys())
            merged.update(record["samples"])
        assert set(merged) == set(leaves)
        assert all(merged[name]["N"] == amount for name, amount in leaves.items())
        proof["categories"][category] = {"leaves": len(merged), "seconds": sum(record["seconds"] for record in records), "samples": merged}
    native = json.loads((results / "native-evidence.json").read_text())
    assert len(native) == 4 and all(record["runtime"] == "runc" for record in native)
    proof["nativeControls"] = native
    proof["dependencyMetadata"] = json.loads((results / "dependency-metadata.json").read_text())
    proof["httpdFinalSampleStaticResponseBodyBytes"] = 15005577216
    proof["httpdCalibrationStaticResponseBodyBytes"] = 50075648
    proof["httpdTotalStaticResponseBodyBytes"] = 15055652864
    proof["expectedPrimaryRequestsIncludingCalibration"] = {"httpd": 10467, "node": 4004, "ruby": 4004}
    proof["redisRequestedOperationBounds"] = {"oneAttempt": 20020, "maximumThreeAttempts": 60060, "reason": "Canonical Redis driver retries infinity output up to three times for each calibration/final invocation; actual retry count is not logged separately."}
    proof["payloadLimits"] = "ExcludesHTTPheaders/404body; Nodeasynchronous0..4KiB+template,Ruby5KiB+template; Redisdefaultvalue/client metadata recorded, setup/RESPbytes not claimedexact. HTTPDN=concurrency onlyonewave; no adequacy/statisticalclaim inferred."
    (results / "pilot-evidence.json").write_text(json.dumps(proof, indent=2) + "\n")
    raise SystemExit(0)
assert mode in ("native", *expected)
images = {name: json.loads((results / f"image-{name}.json").read_text())[0]["Id"] for name in ("httpd", "node", "ruby", "redis", "hey", "util")}
observations = []


def verify(label):
    for path, frozen, target in [(work / "active", results / "base/frozen-fileset.json", "runtime"), (work / "harness", results / "frozen-harness.json", "harness")]:
        command("python3", str(support / "fileset.py"), "verify", str(path), str(frozen), str(results / (label + "-" + target + ".json")), timeout=90)
    for name, identifier in images.items():
        assert command("sudo", "docker", "image", "inspect", "--format", "{{.Id}}", f"gvisor.dev/images/benchmarks/{name}:latest").stdout.strip() == identifier
    config = json.loads(command("sudo", "cat", "/etc/docker/daemon.json").stdout)["runtimes"]["systrap"]
    assert config["path"] == str(work / "active/runsc")
    assert config["runtimeArgs"] == ["--platform=systrap", "--network=sandbox", "--debug=false"]
    command("sudo", "python3", str(support / "quiescent.py"), str(work / "active"), timeout=30)


def parse(path, category):
    parsed = {}
    for line in path.read_text().splitlines():
        if not re.match(r"^Benchmark\S+\s+\d+\s+", line):
            continue
        parts = line.split()
        assert len(parts) >= 8 and len(parts) % 2 == 0 and parts[0] not in parsed, line
        values = {unit: float(value) for value, unit in zip(parts[2::2], parts[3::2])}
        assert all(math.isfinite(value) and value >= 0 for value in values.values()), line
        assert values["ns/op"] > 0, line
        if category == "redis":
            rates = [value for key, value in values.items() if key.endswith((".rps", ".requests_per_second"))]
            assert len(rates) == 1 and rates[0] > 0, line
        else:
            assert values["requests_per_second.QPS"] > 0 and "average_latency.s" in values, line
        parsed[parts[0]] = {"N": int(parts[1]), "metrics": values}
    return parsed


def visit(category, label, selector, count, leaves, runtime, cap):
    global active_process
    assert not containers()
    verify(label + "-before")
    (work / "active-visit").write_text(label + "\n")
    binary = category + "_test"
    runfiles = work / "harness" / (binary + ".runfiles")
    output = results / (label + "-output")
    output.mkdir()
    temporary = work / "bench-tmp"
    temporary.mkdir(exist_ok=True)
    args = ["sudo", "env", f"RUNTIME={runtime}", "GOMAXPROCS=4", f"RUNFILES_DIR={runfiles}", f"TEST_SRCDIR={runfiles}", "TEST_WORKSPACE=_main", f"TMPDIR={temporary}", f"TEST_TMPDIR={temporary}", f"TEST_UNDECLARED_OUTPUTS_DIR={output}", str(work / "harness" / binary), f"-runtime={runtime}", "-test.run=NoTests", f"-test.bench={selector}", f"-test.benchtime={count}x", "-test.count=1", "-test.cpu=4", f"-test.timeout={cap - 10}s"]
    (results / (label + "-argv.json")).write_text(json.dumps(args, indent=2) + "\n")
    path = results / (label + ".log")
    start = time.monotonic()
    with path.open("w") as stream:
        active_process = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            rc = active_process.wait(timeout=cap)
        except subprocess.TimeoutExpired:
            os.killpg(active_process.pid, signal.SIGTERM)
            try:
                active_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(active_process.pid, signal.SIGKILL)
                active_process.wait()
            rc = 124
        active_process = None
    left = containers()
    inspections = json.loads(command("sudo", "docker", "inspect", *left).stdout) if left else []
    (results / (label + "-containers.json")).write_text(json.dumps(inspections, indent=2) + "\n")
    (work / "owned-containers.json").write_text(json.dumps(left) + "\n")
    remove_owned()
    verify(label + "-after")
    raw = path.read_text()
    record = {"category": category, "label": label, "runtime": runtime, "exit": rc, "seconds": time.monotonic() - start, "leftoverContainers": left}
    observations.append(record)
    destination = results / (mode + "-evidence.json")
    destination.write_text(json.dumps(observations, indent=2) + "\n")
    assert rc == 0 and not left and not re.search(r"(^|\s)(FAIL|SKIP)(\s|$)", raw), record
    samples = parse(path, category)
    assert set(samples) == set(leaves) and all(samples[name]["N"] == leaves[name] for name in leaves), samples
    record["samples"] = samples
    destination.write_text(json.dumps(observations, indent=2) + "\n")


try:
    if mode == "native":
        controls = [
            ("httpd", r"BenchmarkHttpd\z/filesize[.]10Kb\z/concurrency[.]1\z", "BenchmarkHttpd/filesize.10Kb/concurrency.1-4"),
            ("node", r"BenchmarkNode\z/concurrency[.]1\z", "BenchmarkNode/concurrency.1-4"),
            ("ruby", r"BenchmarkRuby\z/concurrency[.]1\z", "BenchmarkRuby/concurrency.1-4"),
            ("redis", r"BenchmarkAllRedisOperations\z/operation[.]PING_INLINE\z", "BenchmarkAllRedisOperations/operation.PING_INLINE-4"),
        ]
        for category, selector, name in controls:
            visit(category, "native-" + category, selector, 1000, {name: 1000}, "runc", 180)
    elif mode == "httpd":
        for count in (1, 25, 50, 100, 1000):
            leaves = {name: amount for name, amount in expected[mode].items() if amount == count}
            visit(mode, f"httpd-{count}", rf"Benchmark/filesize/concurrency[.]{count}\z", count, leaves, "systrap", 170)
    else:
        visit(mode, mode, "Benchmark", 1000, expected[mode], "systrap", 840)
finally:
    remove_owned()
