"""Bounded BASE-runtime capability, with exact canonical category accounting."""

import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import stat
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
    subprocess.run(["python3", str(support / "docker_cleanup.py"), str(work), str(results)], check=True, timeout=40)
    command("sudo", "systemctl", "restart", "docker", timeout=20)
    configured = json.loads(command("sudo", "docker", "info", "--format", "{{json .Runtimes}}", timeout=10).stdout)
    original_path = work / "docker-original.json"
    original = json.loads(command("sudo", "cat", str(original_path), timeout=5).stdout) if original_path.exists() else {}
    expected_runtime = original.get("runtimes", {}).get("systrap")
    if expected_runtime is None:
        assert "systrap" not in configured, configured
    else:
        assert configured["systrap"]["path"] == expected_runtime["path"], configured
    (results / "docker-runtime-restoration.json").write_text(json.dumps({"actualRuntimes": configured, "expectedSystrap": expected_runtime, "passed": True}, indent=2) + "\n")
    root_record = work / "public-runtime-root"
    if root_record.exists():
        root = Path(root_record.read_text().strip())
        assert root.parent == Path("/tmp") and re.fullmatch(r"gvisor-tcp-exec\.[A-Za-z0-9]{6}", root.name)
        assert not root.is_symlink() and root.stat().st_uid == os.getuid()
        assert stat.S_IMODE(root.stat().st_mode) == 0o711
        if (root / "active").exists():
            command("sudo", "python3", str(support / "quiescent.py"), str(root / "active"), timeout=10)
        shutil.rmtree(root)
    raise SystemExit(0)
expected = json.loads((support / "expected.json").read_text())
if mode == "analyze":
    proof = {"runtimeSource": "1f33fe3003455db178b97f3b43d3a012f1611994", "harnessSource": os.environ["SOURCE"], "plan": os.environ["PLAN"], "priorExecutionOnlyRun": 44238122262, "priorHttpdExecutionLeavesNotRequestQualified": 33, "categories": {}, "statisticalQualification": False}
    valid = True
    for category, wanted in expected.items():
        path = results / (category + "-evidence.json")
        records = json.loads(path.read_text()) if path.exists() else []
        seen = {}
        for record in records:
            if set(seen) & record.get("samples", {}).keys():
                raise ValueError("Duplicate printed case")
            seen.update(record.get("samples", {}))
        category_valid = set(seen) == set(wanted) and all(record.get("rawProof", {}).get("allFinalValid", False) and record.get("rawProof", {}).get("allRecordsValid", False) and record.get("exit") == 0 for record in records)
        proof["categories"][category] = {"expectedLeaves": len(wanted), "printedLeaves": len(seen), "validRawRequestsAndMetrics": category_valid, "records": records}
        valid &= category_valid
    native = json.loads((results / "native-evidence.json").read_text()) if (results / "native-evidence.json").exists() else []
    high = json.loads((results / "native-high-evidence.json").read_text()) if (results / "native-high-evidence.json").exists() else []
    native_valid = len(native) == 4 and all(record.get("exit") == 0 and record.get("rawProof", {}).get("allFinalValid", False) and record.get("rawProof", {}).get("allRecordsValid", False) for record in native)
    high_valid = len(high) == 1 and high[0].get("exit") == 0 and high[0].get("rawProof", {}).get("allFinalValid", False) and high[0].get("rawProof", {}).get("allRecordsValid", False)
    proof["nativeControls"] = native
    proof["nativeHighControl"] = high
    proof["allSentryFinalRequestsValid"] = valid
    proof["nativeControlsValid"] = native_valid
    proof["nativeHighValid"] = high_valid
    valid &= native_valid and high_valid
    proof["fullCapabilityValid"] = valid
    (results / "recovery-evidence.json").write_text(json.dumps(proof, indent=2) + "\n")
    raise SystemExit(0 if valid else 1)
assert mode in ("native", "native-high", *expected)
images = {name: json.loads((results / f"image-{name}.json").read_text())[0]["Id"] for name in ("httpd", "node", "ruby", "redis", "hey", "util")}
observations = []
public_root = Path((work / "public-runtime-root").read_text().strip())
active = public_root / "active"


def verify(label):
    for path, frozen, target in [(active, results / "base/frozen-fileset.json", "runtime"), (work / "harness", results / "frozen-harness.json", "harness")]:
        command("python3", str(support / "fileset.py"), "verify", str(path), str(frozen), str(results / (label + "-" + target + ".json")), timeout=90)
    for name, identifier in images.items():
        assert command("sudo", "docker", "image", "inspect", "--format", "{{.Id}}", f"gvisor.dev/images/benchmarks/{name}:latest").stdout.strip() == identifier
    config = json.loads(command("sudo", "cat", "/etc/docker/daemon.json").stdout)["runtimes"]["systrap"]
    assert config["path"] == str(active / "runsc")
    assert config["runtimeArgs"] == ["--platform=systrap", "--network=sandbox", "--debug=false"]
    command("sudo", "python3", str(support / "quiescent.py"), str(active), timeout=30)


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
    raw_result = subprocess.run(["sudo", "python3", str(support / "raw_output.py"), str(output / "raw-clients"), category], input=json.dumps(leaves), text=True, capture_output=True, check=True, timeout=60)
    raw_proof = json.loads(raw_result.stdout)
    record["rawProof"] = raw_proof
    destination.write_text(json.dumps(observations, indent=2) + "\n")
    assert rc == 0 and not left and not re.search(r"(^|\s)(FAIL|SKIP)(\s|$)", raw), record
    samples = parse(path, category)
    assert set(samples) == set(leaves) and all(samples[name]["N"] == leaves[name] for name in leaves), samples
    record["samples"] = samples
    destination.write_text(json.dumps(observations, indent=2) + "\n")
    assert raw_proof["allFinalValid"] and raw_proof["allRecordsValid"], raw_proof


try:
    if mode == "native-high":
        name = "BenchmarkContinuousHttpd/filesize.1Mb/concurrency.1000-4"
        visit("httpd", "native-httpd-high", r"BenchmarkContinuousHttpd\z/filesize[.]1Mb\z/concurrency[.]1000\z", 1000, {name: 1000}, "runc", 120)
    elif mode == "native":
        controls = [
            ("httpd", r"BenchmarkHttpd\z/filesize[.]10Kb\z/concurrency[.]1\z", "BenchmarkHttpd/filesize.10Kb/concurrency.1-4"),
            ("node", r"BenchmarkNode\z/concurrency[.]1\z", "BenchmarkNode/concurrency.1-4"),
            ("ruby", r"BenchmarkRuby\z/concurrency[.]1\z", "BenchmarkRuby/concurrency.1-4"),
            ("redis", r"BenchmarkAllRedisOperations\z/operation[.]PING_INLINE\z", "BenchmarkAllRedisOperations/operation.PING_INLINE-4"),
        ]
        native_failed = False
        for category, selector, name in controls:
            try:
                visit(category, "native-" + category, selector, 1000, {name: 1000}, "runc", 180)
            except (AssertionError, ValueError, subprocess.CalledProcessError) as error:
                detail = str(error)
                if isinstance(error, subprocess.CalledProcessError) and error.stderr:
                    detail += "\n" + error.stderr
                if observations and observations[-1].get("category") == category:
                    observations[-1]["validationError"] = detail
                else:
                    observations.append({"category": category, "validationError": detail})
                (results / "native-evidence.json").write_text(json.dumps(observations, indent=2) + "\n")
                print(detail, file=sys.stderr)
                native_failed = True
        if native_failed:
            raise SystemExit(1)
    elif mode == "httpd":
        for count in (1, 25, 50, 100, 1000):
            leaves = {name: amount for name, amount in expected[mode].items() if amount == count}
            visit(mode, f"httpd-{count}", rf"Benchmark/filesize/concurrency[.]{count}\z", count, leaves, "systrap", 300 if count == 1000 else 170)
    else:
        visit(mode, mode, "Benchmark", 1000, expected[mode], "systrap", 840)
finally:
    remove_owned()
