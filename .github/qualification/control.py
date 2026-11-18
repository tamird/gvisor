"""Exercise canonical frozen benchmark harnesses with present/missing readiness input."""

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
work, results = map(Path, sys.argv[2:4])
util = "gvisor.dev/images/benchmarks/util:latest"
active_process = None


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


def command(*args, check=True, timeout=30):
    return subprocess.run(args, text=True, capture_output=True, check=check, timeout=timeout)


def containers():
    return command("sudo", "docker", "ps", "-aq").stdout.split()


def remove_owned():
    # Every visit starts with an empty daemon on this exclusive disposable job.
    # An interrupted active visit can therefore leave only this task's containers.
    owned = set(json.loads((work / "owned-containers.json").read_text())) if (work / "owned-containers.json").exists() else set()
    if (work / "active-visit").exists():
        owned.update(containers())
    for identifier in sorted(owned):
        command("sudo", "docker", "rm", "-f", identifier, check=False)
    (work / "owned-containers.json").write_text("[]\n")
    (work / "active-visit").unlink(missing_ok=True)


def restore_util():
    record = work / "util-id.txt"
    if record.exists():
        identifier = record.read_text().strip()
        command("sudo", "docker", "image", "tag", identifier, util)
        assert command("sudo", "docker", "image", "inspect", "--format", "{{.Id}}", util).stdout.strip() == identifier
        rescue = work / "util-rescue.txt"
        if rescue.exists():
            command("sudo", "docker", "image", "rm", rescue.read_text().strip())
            rescue.unlink()


if mode == "cleanup":
    remove_owned()
    restore_util()
    (results / "control-cleanup.json").write_text(json.dumps({"utilRestored": True, "remainingContainers": containers()}, indent=2) + "\n")
    raise SystemExit(0)
if mode not in ("run", "positive-fixed"):
    raise ValueError(mode)
support = Path(sys.argv[4])
assert not containers()
images = {}
for name in ("nginx", "httpd", "node", "redis", "hey", "util"):
    records = json.loads((results / f"image-{name}.json").read_text())
    assert len(records) == 1
    images[name] = records[0]["Id"]
(work / "util-id.txt").write_text(images["util"] + "\n")
rescue = "localhost/gvisor-readiness-util:" + os.environ["GITHUB_RUN_ID"]
command("sudo", "docker", "image", "tag", images["util"], rescue)
(work / "util-rescue.txt").write_text(rescue + "\n")
checks = []


def verify(arm, label, util_present):
    command("python3", str(support / "fileset.py"), "verify", str(work / arm), str(results / f"fileset-{arm}.json"), str(results / f"{label}-harness.json"), timeout=90)
    for name, identifier in images.items():
        actual = command("sudo", "docker", "image", "inspect", "--format", "{{.Id}}", f"gvisor.dev/images/benchmarks/{name}:latest", check=False)
        if name == "util" and not util_present:
            assert actual.returncode != 0, actual
        else:
            assert actual.returncode == 0 and actual.stdout.strip() == identifier, (name, actual)


def samples(path):
    parsed = {}
    for line in path.read_text().splitlines():
        if not re.match(r"^Benchmark\S+\s+\d+\s+", line):
            continue
        parts = line.split()
        assert len(parts) >= 8 and len(parts) % 2 == 0, line
        assert parts[0] not in parsed, line
        values = {unit: float(value) for value, unit in zip(parts[2::2], parts[3::2])}
        assert all(math.isfinite(value) and value > 0 for value in values.values()), line
        assert {"ns/op", "requests_per_second.QPS", "average_latency.s"} <= values.keys(), line
        parsed[parts[0]] = {"N": int(parts[1]), "metrics": values}
    return parsed


cases = [
    ("nginx", "nginx_test", r"BenchmarkNginxDocSize\z/filesize[.]10Kb\z/concurrency[.]1\z/filesystem[.]tmpfs\z", 10000, {"BenchmarkNginxDocSize/filesize.10Kb/concurrency.1/filesystem.tmpfs-4"}),
    ("httpd", "httpd_test", r"BenchmarkHttpd\z/filesize[.]10Kb\z/concurrency[.]1\z", 1000, {"BenchmarkHttpd/filesize.10Kb/concurrency.1-4"}),
    ("node", "node_test", r"BenchmarkNode\z", 1000, {f"BenchmarkNode/concurrency.{n}-4" for n in (1, 5, 10, 25)}),
]


def visit(arm, category, binary, selector, count, expected, missing):
    global active_process
    label = f"{'missing' if missing else 'present'}-{arm}-{category}"
    assert not containers()
    verify(arm, label + "-before", not missing)
    (work / "active-visit").write_text(label + "\n")
    runfiles = work / arm / (binary + ".runfiles")
    output = results / (label + "-output")
    output.mkdir()
    temporary = work / "bench-tmp"
    temporary.mkdir(exist_ok=True)
    args = ["sudo", "env", "RUNTIME=runc", "GOMAXPROCS=4", f"RUNFILES_DIR={runfiles}", f"TEST_SRCDIR={runfiles}", "TEST_WORKSPACE=_main", f"TMPDIR={temporary}", f"TEST_TMPDIR={temporary}", f"TEST_UNDECLARED_OUTPUTS_DIR={output}", str(work / arm / binary), "-runtime=runc", "-test.run=NoTests", f"-test.bench={selector}", f"-test.benchtime={count}x", "-test.count=1", "-test.cpu=4", "-test.timeout=150s"]
    (results / (label + "-argv.json")).write_text(json.dumps(args, indent=2) + "\n")
    path = results / (label + ".log")
    start = time.monotonic()
    with path.open("w") as stream:
        process = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        active_process = process
        try:
            rc = process.wait(timeout=180)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            rc = 124
        active_process = None
    remaining = containers()
    inspections = json.loads(command("sudo", "docker", "inspect", *remaining).stdout) if remaining else []
    (results / (label + "-containers.json")).write_text(json.dumps(inspections, indent=2) + "\n")
    (work / "owned-containers.json").write_text(json.dumps(remaining) + "\n")
    remove_owned()
    verify(arm, label + "-after", not missing)
    raw = path.read_text()
    observation = {"label": label, "arm": arm, "missingUtil": missing, "exit": rc, "seconds": time.monotonic() - start, "leftoverContainerIDs": remaining}
    checks.append(observation)
    (results / "control-evidence.json").write_text(json.dumps(checks, indent=2) + "\n")
    assert rc not in (124, 137, -signal.SIGTERM, -signal.SIGKILL), observation
    if missing:
        if arm == "fixed":
            assert rc != 0 and "server readiness failed:" in raw and "gvisor.dev/images/benchmarks/util" in raw, observation
            assert not re.search(r"^Benchmark\S+\s+\d+\s+", raw, re.MULTILINE), raw
            assert not remaining, observation
        # BASE outcome is preserved exactly; do not require exit0 or retry into it.
    else:
        assert rc == 0 and not re.search(r"(^|\s)(FAIL|SKIP)(\s|$)", raw), observation
        parsed = samples(path)
        assert set(parsed) == expected and all(value["N"] == count for value in parsed.values()), parsed
        observation["samples"] = parsed
        if arm == "fixed":
            assert not remaining, observation
        elif category == "node":
            assert inspections and all(record["Image"] == images["hey"] and record["Config"]["Cmd"][0] == "hey" for record in inspections), inspections
        else:
            assert not remaining, observation
    (results / "control-evidence.json").write_text(json.dumps(checks, indent=2) + "\n")


try:
    if mode == "run":
        command("sudo", "docker", "image", "rm", util)
        for arm in ("base", "fixed"):
            for case in cases:
                visit(arm, *case, missing=True)
        command("sudo", "docker", "image", "tag", images["util"], util)
        for case in cases:
            visit("base", *case, missing=False)
    for case in cases:
        visit("fixed", *case, missing=False)
    (results / "controls-complete.json").write_text(json.dumps({"passed": True, "visits": len(checks), "scope": mode, "runtime": "runc", "performanceComparison": False, "nodeHistoricalTimingComparable": False}, indent=2) + "\n")
finally:
    remove_owned()
    restore_util()
