#!/usr/bin/env python3
"""Run the existing startup benchmark against two complete runtime builds.

This is the fixed-pair qualification runner adapted for Docker startup. Docker
starts the runtime outside the benchmark's process tree, so harness CPU/RSS
measurements would not describe the runtime and are deliberately not collected.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class Sample:
    name: str
    side: str
    benchtime: str
    measured: bool


def cohort() -> list[Sample]:
    rows = [Sample("warmup-" + side, side, "100ms", False)
            for side in ("before", "after")]
    for block in range(16):
        for side in (("before", "after") if block % 2 == 0 else ("after", "before")):
            rows.append(Sample(f"sample-{block:02d}-{side}", side, "1s", True))
    return rows


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def containers() -> set[str]:
    return set(subprocess.check_output(
        ["docker", "ps", "--all", "--quiet", "--no-trunc"], text=True, timeout=10).split())


def main(binary: Path, reporter: Path) -> int:
    package = Path(__file__).resolve().parent
    output = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    xml = Path(os.environ["XML_OUTPUT_FILE"])
    manifest = json.loads((package / "inputs.json").read_text())
    observed = {"path": str(binary), "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "bytes": binary.stat().st_size}
    write_json(output / "harness-identity.json", {"expected": manifest["harness"], "actual": observed})
    if (observed["sha256"] != manifest["harness"]["sha256"] or
            observed["bytes"] != manifest["harness"]["bytes"] or
            binary != Path(manifest["harness"]["resolved_path"])):
        raise ValueError(f"Common startup harness differs: {observed!r} != {manifest['harness']!r}")
    if (reporter.stat().st_size != manifest["benchstat"]["bytes"] or
            hashlib.sha256(reporter.read_bytes()).hexdigest() != manifest["benchstat"]["sha256"]):
        raise ValueError("The benchstat executable differs from its configured output")
    configuration = json.loads(Path("/etc/docker/daemon.json").read_text())
    for side in ("before", "after"):
        runtime = manifest["runtimes"][side]
        if configuration["runtimes"][runtime["name"]] != runtime["configuration"]:
            raise ValueError("Runtime registration changed: " + side)
        for row in runtime["files"]:
            path = Path(row["path"])
            if path.stat().st_size != row["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("Runtime file changed: " + str(path))
    image = subprocess.check_output(
        ["docker", "image", "inspect", manifest["image"]["tag"], "--format={{.Id}}"],
        text=True, timeout=10).strip()
    if image != manifest["image"]["id"]:
        raise ValueError("Startup fixture image changed")
    initial_containers = containers()
    plan = cohort()
    write_json(output / "selection.json", [asdict(row) for row in plan])
    write_json(output / "inputs.json", manifest)
    write_json(output / "host.json", {
        "uname": list(os.uname()), "cpuinfo": Path("/proc/cpuinfo").read_text(),
        "allowedCpus": sorted(os.sched_getaffinity(0)),
        "bootID": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "initialContainers": sorted(initial_containers),
    })
    cancellation = 0
    process = None
    completed = []
    benchfiles = {"before": [], "after": []}
    status = 1

    def cancel(signum, _frame):
        nonlocal cancellation
        cancellation = 128 + signum
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        for sample in plan:
            if (binary.stat().st_size != observed["bytes"] or
                    hashlib.sha256(binary.read_bytes()).hexdigest() != observed["sha256"]):
                raise ValueError("Common startup harness changed before " + sample.name)
            if cancellation:
                return cancellation
            if containers() != initial_containers:
                raise RuntimeError("Container inventory changed before " + sample.name)
            directory = output / sample.name
            directory.mkdir()
            (directory / "tmp").mkdir()
            argv = [str(binary), "-test.run=^$", "-test.bench=^BenchmarkStartupEmpty$",
                    "-test.benchtime=" + sample.benchtime, "-test.benchmem", "-test.count=1",
                    "-test.cpu=1", "-test.v=false", "-runtime=" + manifest["runtimes"][sample.side]["name"],
                    "-config_path=/etc/docker/daemon.json"]
            environment = dict(os.environ, GOMAXPROCS="1", GO_TEST_WRAP="1",
                GO_TEST_RUN_FROM_BAZEL="1", TEST_BINARY=str(binary),
                TEST_TMPDIR=str(directory / "tmp"), XML_OUTPUT_FILE=str(directory / "test.xml"),
                TEST_UNDECLARED_OUTPUTS_DIR=str(directory))
            started = time.monotonic()
            timed_out = False
            with (directory / "test.log").open("wb") as log:
                process = subprocess.Popen(argv, env=environment, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    while process.poll() is None and not cancellation:
                        remaining = 60 - (time.monotonic() - started)
                        if remaining <= 0:
                            timed_out = True
                            break
                        try:
                            process.wait(timeout=min(remaining, 1))
                        except subprocess.TimeoutExpired:
                            pass
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)
                status = cancellation or (124 if timed_out else process.returncode if process.returncode >= 0 else 128 - process.returncode)
                if cancellation or timed_out:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process = None
            row = dict(asdict(sample), status=status, argv=argv,
                       elapsedSeconds=time.monotonic() - started)
            write_json(directory / "status.json", row)
            text = (directory / "test.log").read_text().replace("\x16", "")
            print(f"PAIR_RAW_BEGIN {sample.name}\n{text}\nPAIR_RAW_END {sample.name}", flush=True)
            if status:
                return status
            status = 1
            if containers() != initial_containers:
                raise RuntimeError("Benchmark cleanup left containers after " + sample.name)
            if not re.search(r"^PASS$", text, re.M) or re.search(r"^FAIL(?:\s|$)", text, re.M):
                raise ValueError("Benchmark did not report PASS")
            document = ET.parse(directory / "test.xml")
            if (any(document.findall(".//" + tag) for tag in ("failure", "error", "skipped")) or
                    any(int(node.get(key, "0")) for node in document.iter() for key in ("failures", "errors", "skipped"))):
                raise ValueError("Benchmark XML reports a failure or skip")
            lines = [line for line in text.splitlines() if re.match(r"^Benchmark\S+\s+\d+\s+", line)]
            if len(lines) != 1 or re.sub(r"-1$", "", lines[0].split()[0]) != "BenchmarkStartupEmpty":
                raise ValueError("Expected exactly one startup benchmark result")
            parts = lines[0].split()
            metrics = {parts[i + 1]: float(parts[i]) for i in range(2, len(parts), 2)}
            if (int(parts[1]) <= 0 or not {"ns/op", "B/op", "allocs/op"} <= metrics.keys() or
                    not all(math.isfinite(value) and value >= 0 for value in metrics.values()) or metrics["ns/op"] <= 0):
                raise ValueError("Invalid benchmark measurements")
            row.update(metrics=metrics, row=lines[0])
            completed.append(row)
            if sample.measured:
                benchfiles[sample.side].append(lines[0])
        write_json(output / "process-results.json", completed)
        for side, lines in benchfiles.items():
            (output / (side + ".bench")).write_text(
                "goos: linux\ngoarch: amd64\npkg: gvisor.dev/gvisor/test/benchmarks/base\n" + "\n".join(lines) + "\n")
        result = subprocess.run([str(reporter), str(output / "before.bench"), str(output / "after.bench")],
                                check=True, text=True, capture_output=True, timeout=60)
        (output / "benchstat.txt").write_text(result.stdout)
        print(result.stdout, flush=True)
        status = 0
        return cancellation or status
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                status = status or 1
                write_json(output / "process-cleanup.json", {
                    "pid": process.pid, "status": "FAIL",
                    "error": "Benchmark process did not exit within five seconds after SIGKILL",
                })
        leaked = containers() - initial_containers
        if leaked:
            status = status or 1
            result = subprocess.run(["docker", "rm", "--force", *sorted(leaked)],
                                    capture_output=True, text=True, timeout=30)
            write_json(output / "container-cleanup.json", {
                "taskCreated": sorted(leaked), "nativeExit": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr,
            })
        failed = bool(cancellation or status or len(completed) != len(plan) or containers() != initial_containers)
        document = ET.Element("testsuites")
        suite = ET.SubElement(document, "testsuite", name="startup-benchmark-processes",
                              tests=str(len(completed) + int(failed)), failures=str(int(failed)), errors="0")
        for row in completed:
            ET.SubElement(suite, "testcase", name=row["name"], time=str(row["elapsedSeconds"]))
        if failed:
            case = ET.SubElement(suite, "testcase", name="incomplete-cohort")
            ET.SubElement(case, "failure").text = "Native execution, evidence, or cleanup failed."
        ET.ElementTree(document).write(xml, encoding="utf-8", xml_declaration=True)
        if failed:
            return cancellation or status or 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve(strict=True), Path(sys.argv[2]).resolve(strict=True)))
