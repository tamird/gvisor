#!/usr/bin/env python3
"""Run one existing benchmark group against two immutable fixture images.

This reuses the fixed-pair qualification runner for Docker fixtures. Docker
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


def cohort(benchtime: str) -> list[Sample]:
    rows = [Sample("warmup-" + side, side, benchtime, False)
            for side in ("before", "after")]
    for block in range(16):
        for side in (("before", "after") if block % 2 == 0 else ("after", "before")):
            rows.append(Sample(f"sample-{block:02d}-{side}", side, benchtime, True))
    return rows


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def containers() -> set[str]:
    return set(subprocess.check_output(
        ["docker", "ps", "--all", "--quiet", "--no-trunc"], text=True, timeout=10).split())


def measurements(text: str, expected: dict) -> tuple[list[str], list[dict]]:
    """Reject partial, duplicate, malformed, or failed native benchmark output."""
    lines = []
    passed = False
    for line in text.splitlines():
        if line == "PASS":
            if passed:
                raise ValueError("Benchmark reported PASS twice")
            passed = True
        elif re.match(r"^FAIL(?:\s|$)", line):
            raise ValueError("Benchmark reported FAIL")
        elif line.startswith("Benchmark") and len(line.split()) > 1:
            if passed:
                raise ValueError("Benchmark result follows PASS")
            lines.append(line)
    if not passed:
        raise ValueError("Benchmark did not report PASS")
    measurements = []
    seen = set()
    for line in lines:
        parts = line.split()
        name = re.sub(r"-[0-9]+$", "", parts[0])
        if name not in expected or name in seen or len(parts) < 4 or len(parts) % 2 or not parts[1].isdecimal():
            raise ValueError("Unexpected or duplicate benchmark result: " + line)
        seen.add(name)
        metrics = {parts[i + 1]: float(parts[i]) for i in range(2, len(parts), 2)}
        wanted = expected[name]
        if (int(parts[1]) != wanted["iterations"] or
                len(metrics) != (len(parts) - 2) // 2 or set(metrics) != set(wanted["units"]) or
                not all(math.isfinite(value) for value in metrics.values()) or
                metrics["ns/op"] <= 0):
            raise ValueError("Invalid benchmark measurements: " + line)
        measurements.append({"name": name, "iterations": int(parts[1]), "metrics": metrics, "row": line})
    if seen != set(expected):
        raise ValueError(f"Incomplete benchmark group: {seen!r} != {set(expected)!r}")
    return lines, measurements


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
        raise ValueError(f"Common benchmark harness differs: {observed!r} != {manifest['harness']!r}")
    if (reporter.stat().st_size != manifest["benchstat"]["bytes"] or
            hashlib.sha256(reporter.read_bytes()).hexdigest() != manifest["benchstat"]["sha256"]):
        raise ValueError("The benchstat executable differs from its configured output")
    if manifest["runtimes"]["before"] != manifest["runtimes"]["after"]:
        raise ValueError("Image comparisons require one identical complete runtime")
    configuration = json.loads(Path("/etc/docker/daemon.json").read_text())
    for side in ("before", "after"):
        runtime = manifest["runtimes"][side]
        if configuration["runtimes"][runtime["name"]] != runtime["configuration"]:
            raise ValueError("Runtime registration changed: " + side)
        for row in runtime["files"]:
            path = Path(row["path"])
            if path.stat().st_size != row["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("Runtime file changed: " + str(path))
    if manifest["child_timeout_seconds"] != 300:
        raise ValueError("Expected the reviewed 300-second Apache child bound")
    group = manifest["benchmark"]
    expected = {row["name"]: row for row in group["rows"]}
    if (not expected or len(expected) != len(group["rows"]) or
            group["benchtime"] not in ("1x", "100x") or
            any(row["iterations"] != int(group["benchtime"][:-1]) or
                "ns/op" not in row["units"] or len(row["units"]) != len(set(row["units"]))
                for row in expected.values())):
        raise ValueError("Expected one explicit fixed-iteration benchmark group")
    image = manifest["image"]

    def image_id(reference: str) -> str:
        return subprocess.check_output(
            ["docker", "image", "inspect", reference, "--format={{.Id}}"],
            text=True, timeout=10).strip()

    helper_tags = [helper["tag"] for helper in manifest["helpers"]]
    if len(helper_tags) != len(set(helper_tags)) or image["tag"] in helper_tags:
        raise ValueError("Fixed helper tags must be unique and distinct from the varying image")
    original_image = image_id(image["tag"])
    for side in ("before", "after"):
        if image_id(image[side]["id"]) != image[side]["id"]:
            raise ValueError("Benchmark image arm changed: " + side)
    for helper in manifest["helpers"]:
        if image_id(helper["tag"]) != helper["id"]:
            raise ValueError("Benchmark helper changed: " + helper["tag"])
    initial_containers = containers()
    plan = cohort(group["benchtime"])
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
                raise ValueError("Common benchmark harness changed before " + sample.name)
            if cancellation:
                return cancellation
            if containers() != initial_containers:
                raise RuntimeError("Container inventory changed before " + sample.name)
            arm = image[sample.side]["id"]
            subprocess.run(["docker", "tag", arm, image["tag"]], check=True, timeout=10)
            if image_id(image["tag"]) != arm:
                raise ValueError("Selected fixture image differs before " + sample.name)
            for helper in manifest["helpers"]:
                if image_id(helper["tag"]) != helper["id"]:
                    raise ValueError("Benchmark helper changed before " + sample.name)
            directory = output / sample.name
            directory.mkdir()
            (directory / "tmp").mkdir()
            argv = [str(binary), "-test.run=^$", "-test.bench=" + group["pattern"],
                    "-test.benchtime=" + sample.benchtime, "-test.count=1", "-test-checkpoint=true",
                    "-test.cpu=1", "-test.v=false", "-runtime=" + manifest["runtimes"][sample.side]["name"],
                    "-config_path=/etc/docker/daemon.json"]
            environment = dict(os.environ, GOMAXPROCS="1", GO_TEST_WRAP="1", GO_TEST_WRAP_TESTV="0",
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
                        remaining = manifest["child_timeout_seconds"] - (time.monotonic() - started)
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
            row = dict(asdict(sample), image=arm, status=status, argv=argv,
                       elapsedSeconds=time.monotonic() - started)
            write_json(directory / "status.json", row)
            text = (directory / "test.log").read_text().replace("\x16", "")
            print(f"PAIR_RAW_BEGIN {sample.name}\n{text}\nPAIR_RAW_END {sample.name}", flush=True)
            if status:
                return status
            status = 1
            if containers() != initial_containers:
                raise RuntimeError("Benchmark cleanup left containers after " + sample.name)
            if image_id(image["tag"]) != arm:
                raise ValueError("Selected fixture image differs after " + sample.name)
            for helper in manifest["helpers"]:
                if image_id(helper["tag"]) != helper["id"]:
                    raise ValueError("Benchmark helper changed after " + sample.name)
            document = ET.parse(directory / "test.xml")
            if (any(document.findall(".//" + tag) for tag in ("failure", "error", "skipped")) or
                    any(int(node.get(key, "0")) for node in document.iter() for key in ("failures", "errors", "skipped"))):
                raise ValueError("Benchmark XML reports a failure or skip")
            lines, values = measurements(text, expected)
            row["measurements"] = values
            completed.append(row)
            if sample.measured:
                benchfiles[sample.side].extend(lines)
        write_json(output / "process-results.json", completed)
        for side, lines in benchfiles.items():
            (output / (side + ".bench")).write_text(
                "goos: linux\ngoarch: amd64\npkg: " + group["package"] + "\n" + "\n".join(lines) + "\n")
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
        restored = subprocess.run(["docker", "tag", original_image, image["tag"]],
                                  capture_output=True, text=True, timeout=10)
        write_json(output / "image-cleanup.json", {"tag": image["tag"], "original": original_image,
                   "nativeExit": restored.returncode, "stdout": restored.stdout, "stderr": restored.stderr})
        failed = bool(cancellation or status or len(completed) != len(plan) or
                      containers() != initial_containers or restored.returncode or
                      image_id(image["tag"]) != original_image)
        document = ET.Element("testsuites")
        suite = ET.SubElement(document, "testsuite", name="image-benchmark-processes",
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
