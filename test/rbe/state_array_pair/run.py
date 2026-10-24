#!/usr/bin/env python3
"""Run the checkpoint benchmark with matching per-side binaries and runtimes.

The benchmark owns its GC and timing boundaries. Each fresh process checkpoints
one common image using the runtime built from the same source as its executable.
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
from typing import TypedDict
import xml.etree.ElementTree as ET


class FileIdentity(TypedDict):
    bytes: int
    sha256: str


class Runfile(FileIdentity):
    path: str


class Harness(TypedDict):
    source: str
    owner: dict[str, str]
    benchmark_source_sha256: str
    binary: str
    runfiles_root: str
    files: dict[str, FileIdentity]


class Runtime(TypedDict):
    name: str
    source: str
    configuration: dict[str, object]
    files: list[Runfile]


class Image(TypedDict):
    tag: str
    id: str


@dataclass(frozen=True)
class Sample:
    name: str
    side: str
    benchtime: str
    measured: bool


def cohort() -> list[Sample]:
    rows = [Sample("warmup-" + side, side, "1x", False)
            for side in ("before", "after")]
    for block in range(16):
        for side in (("before", "after") if block % 2 == 0 else ("after", "before")):
            rows.append(Sample(f"sample-{block:02d}-{side}", side, "1x", True))
    return rows


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def containers() -> set[str]:
    return set(subprocess.check_output(
        ["docker", "ps", "--all", "--quiet", "--no-trunc"], text=True, timeout=10).split())


def checked_file(path: Path, identity: FileIdentity) -> None:
    if (path.stat().st_size != identity["bytes"] or
            hashlib.sha256(path.read_bytes()).hexdigest() != identity["sha256"]):
        raise ValueError("File identity changed: " + str(path))


def check_harness(package: Path, harness: Harness) -> tuple[Path, Path]:
    root = (package / harness["runfiles_root"]).resolve(strict=True)
    root.relative_to(package)
    binary = (package / harness["binary"]).resolve(strict=True)
    binary.relative_to(package)
    expected = harness["files"]
    if not expected or harness["binary"] not in expected:
        raise ValueError("Benchmark is absent from its file inventory")
    actual = {str(path.relative_to(package)) for path in root.rglob("*") if path.is_file()}
    actual.add(harness["binary"])
    if actual != expected.keys():
        raise ValueError("Benchmark runfiles inventory changed")
    for relative, identity in expected.items():
        path = (package / relative).resolve(strict=True)
        path.relative_to(package)
        checked_file(path, identity)
    if not os.access(binary, os.X_OK):
        raise ValueError("Benchmark is not executable")
    if not (root / "_main/test/benchmarks/checkpointfile").is_dir():
        raise ValueError("Benchmark package runfiles directory is absent")
    return binary, root


def check_runtime(runtime: Runtime) -> None:
    configuration = json.loads(Path("/etc/docker/daemon.json").read_text())
    if configuration["runtimes"][runtime["name"]] != runtime["configuration"]:
        raise ValueError("Runtime registration changed")
    for row in runtime["files"]:
        checked_file(Path(row["path"]), row)


def check_image(image: Image) -> None:
    actual = subprocess.check_output(
        ["docker", "image", "inspect", image["tag"], "--format={{.Id}}"],
        text=True, timeout=10).strip()
    if actual != image["id"]:
        raise ValueError("Checkpoint fixture image changed")


def check_daemon(expected: dict) -> dict:
    pid = int(Path("/var/run/docker.pid").read_text())
    process = Path("/proc") / str(pid)
    executable = (process / "exe").resolve(strict=True)
    ticks = (process / "stat").read_text().rsplit(")", 1)[1].split()[19]
    if (pid != expected["pid"] or ticks != expected["start_ticks"] or
            str(executable) != expected["executable"]["path"]):
        raise ValueError("Qualified Docker process changed")
    checked_file(executable, expected["executable"])
    return {"pid": pid, "start_ticks": ticks, "executable": expected["executable"]}


def main(reporter: Path) -> int:
    package = Path(__file__).resolve().parent
    output = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    xml = Path(os.environ["XML_OUTPUT_FILE"])
    manifest = json.loads((package / "inputs.json").read_text())
    if os.uname().machine != "x86_64" or os.geteuid() != 0:
        raise ValueError("Checkpoint benchmark requires a root AMD64 worker")
    if set(manifest["harnesses"]) != {"before", "after"} or set(manifest["runtimes"]) != {"before", "after"}:
        raise ValueError("Expected exactly two source-matched arms")
    if "codec_modes" in manifest and manifest["codec_modes"] != {"before": "legacy", "after": "typed"}:
        raise ValueError("Expected legacy versus typed codec selection")
    observed = {}
    for side in ("before", "after"):
        harness = manifest["harnesses"][side]
        runtime = manifest["runtimes"][side]
        if (not re.fullmatch(r"[0-9a-f]{40}", harness["source"]) or harness["source"] != runtime["source"] or
                harness["owner"]["label"] != "//test/benchmarks/checkpointfile:checkpointfile_test" or
                not harness["owner"]["configuration"] or
                harness["runfiles_root"] != "arms/" + side + "/benchmark.runfiles" or
                harness["binary"] != "arms/" + side + "/benchmark" or
                not re.fullmatch(r"[0-9a-f]{64}", harness["benchmark_source_sha256"])):
            raise ValueError("Benchmark/runtime source or configured owner differs: " + side)
        binary, runfiles = check_harness(package, harness)
        observed[side] = {"path": str(binary), "runfiles_root": str(runfiles),
                          **harness["files"][harness["binary"]],
                          "source": harness["source"], "owner": harness["owner"]}
        check_runtime(runtime)
    if manifest["harnesses"]["before"]["benchmark_source_sha256"] != manifest["harnesses"]["after"]["benchmark_source_sha256"]:
        raise ValueError("Benchmark source must be identical across arms")
    write_json(output / "harness-identity.json", observed)
    checked_file(reporter, manifest["benchstat"])
    check_image(manifest["image"])
    check_daemon(manifest["daemon"])
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
    for name in ("selection.json", "inputs.json", "harness-identity.json", "host.json"):
        print(f"PAIR_INPUT {name} " + (output / name).read_text(), flush=True)
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
            harness = manifest["harnesses"][sample.side]
            runtime = manifest["runtimes"][sample.side]
            binary, runfiles = check_harness(package, harness)
            check_runtime(runtime)
            check_image(manifest["image"])
            daemon_before = check_daemon(manifest["daemon"])
            if cancellation:
                return cancellation
            if containers() != initial_containers:
                raise RuntimeError("Container inventory changed before " + sample.name)
            directory = output / sample.name
            directory.mkdir()
            (directory / "tmp").mkdir()
            argv = [str(binary), "-test.run=^$", "-test.bench=^BenchmarkCheckpointFile$",
                    "-test.benchtime=" + sample.benchtime, "-measure-lifecycle", "-test.count=1",
                    "-test.cpu=1", "-test.v=false", "-runtime=" + manifest["runtimes"][sample.side]["name"],
                    "-config_path=/etc/docker/daemon.json", "-test-checkpoint=true",
                    "-oci-bundle-root=" + manifest["oci_bundle_root"]]
            if "codec_modes" in manifest:
                argv.append("-expected-codec=" + manifest["codec_modes"][sample.side])
            environment = dict(os.environ, GOMAXPROCS="1", GO_TEST_WRAP="1",
                GO_TEST_RUN_FROM_BAZEL="1", TEST_BINARY=str(binary),
                TEST_TMPDIR=str(directory / "tmp"), XML_OUTPUT_FILE=str(directory / "test.xml"),
                TEST_UNDECLARED_OUTPUTS_DIR=str(directory), TEST_SRCDIR=str(runfiles),
                RUNFILES_DIR=str(runfiles), TEST_WORKSPACE="_main")
            environment.pop("RUNFILES_MANIFEST_FILE", None)
            started = time.monotonic()
            timed_out = False
            with (directory / "test.log").open("wb") as log:
                process = subprocess.Popen(argv, env=environment, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    while process.poll() is None and not cancellation:
                        remaining = 180 - (time.monotonic() - started)
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
            print(f"PAIR_RAW_BEGIN {sample.name}\n{text}", flush=True)
            for name in ("status.json", "test.xml"):
                path = directory / name
                if path.exists():
                    print(f"PAIR_FILE {sample.name} {name} " + path.read_text(), flush=True)
            print(f"PAIR_RAW_END {sample.name}", flush=True)
            if status:
                return status
            status = 1
            check_harness(package, harness)
            check_runtime(runtime)
            check_image(manifest["image"])
            daemon_after = check_daemon(manifest["daemon"])
            row["daemon"] = {"before": daemon_before, "after": daemon_after}
            if containers() != initial_containers:
                raise RuntimeError("Benchmark cleanup left containers after " + sample.name)
            if not re.search(r"^PASS$", text, re.M) or re.search(r"^FAIL(?:\s|$)", text, re.M):
                raise ValueError("Benchmark did not report PASS")
            document = ET.parse(directory / "test.xml")
            if (any(document.findall(".//" + tag) for tag in ("failure", "error", "skipped")) or
                    any(int(node.get(key, "0")) for node in document.iter() for key in ("failures", "errors", "skipped"))):
                raise ValueError("Benchmark XML reports a failure or skip")
            lines = [line for line in text.splitlines() if re.match(r"^Benchmark\S+\s+\d+\s+", line)]
            expected = {"BenchmarkCheckpointFile/" + name: {"ns/op"}
                        for name in ("Checkpoint", "Restore", "RestoreHTTP")}
            measured = {}
            for line in lines:
                parts = line.split()
                name = re.sub(r"-1$", "", parts[0])
                if name not in expected or name in measured or len(parts) % 2:
                    raise ValueError("Unexpected or duplicate checkpoint benchmark row")
                metrics = {parts[i + 1]: float(parts[i]) for i in range(2, len(parts), 2)}
                if (len(metrics) != (len(parts) - 2) // 2 or set(metrics) != expected[name] or
                        int(parts[1]) != 1 or
                        not all(math.isfinite(value) and value >= 0 for value in metrics.values()) or
                        metrics["ns/op"] <= 0):
                    raise ValueError("Invalid checkpoint benchmark measurements")
                measured[name] = {"iterations": int(parts[1]), "metrics": metrics, "row": line}
            if measured.keys() != expected.keys():
                raise ValueError("Expected one checkpoint, restore and restored HTTP measurement")
            if measured["BenchmarkCheckpointFile/RestoreHTTP"]["metrics"]["ns/op"] < measured["BenchmarkCheckpointFile/Restore"]["metrics"]["ns/op"]:
                raise ValueError("HTTP readiness precedes Docker restore")
            if "codec_modes" in manifest:
                codec = json.loads((directory / "codec-mode.json").read_text())
                mode = manifest["codec_modes"][sample.side]
                if codec["expected"] != mode or codec["runtime"] != runtime["name"]:
                    raise ValueError("Codec observation differs from selected arm")
                for name in ("kernel_input_header", "kernel_output_header"):
                    header = codec[name]
                    if (type(header) is not int or header <= 0 or header >= 1 << 63 or
                            bool(header & (1 << 62)) != (mode == "typed")):
                        raise ValueError("Kernel stream used the wrong codec")
                for direction in ("input", "output"):
                    if (type(codec[direction + "_bytes"]) is not int or codec[direction + "_bytes"] <= 0 or
                            not re.fullmatch(r"[0-9a-f]{64}", codec[direction + "_sha256"])):
                        raise ValueError("Missing codec probe stream identity")
                row["codec"] = codec
            row.update(benchmarks=measured, source=harness["source"], harness=observed[sample.side],
                       runtime=runtime["name"])
            completed.append(row)
            if sample.measured:
                benchfiles[sample.side].extend(lines)
        write_json(output / "process-results.json", completed)
        print("PAIR_RESULTS " + json.dumps(completed), flush=True)
        for side, lines in benchfiles.items():
            (output / (side + ".bench")).write_text(
                "goos: linux\ngoarch: amd64\npkg: gvisor.dev/gvisor/test/benchmarks/checkpointfile\n" + "\n".join(lines) + "\n")
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
        final_containers = containers()
        if final_containers != initial_containers:
            status = status or 1
            write_json(output / "container-cleanup.json", {
                "status": "FAIL", "initial": sorted(initial_containers),
                "final": sorted(final_containers),
                "error": "Container inventory changed; unknown containers were not removed.",
            })
        failed = bool(cancellation or status or len(completed) != len(plan) or final_containers != initial_containers)
        document = ET.Element("testsuites")
        suite = ET.SubElement(document, "testsuite", name="checkpoint-benchmark-processes",
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
    raise SystemExit(main(Path(sys.argv[1]).resolve(strict=True)))
