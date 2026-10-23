"""Exercise the seven syscallbench modes against the inspected scratch image."""
import json
import math
from pathlib import Path
import re
import subprocess
import uuid

from image_runtime import prepared_runtime


def run(invoke, evidence: Path, private: Path) -> None:
    expected = {"BenchmarkSyscallbench/syscall.getpid", "BenchmarkSyscallbench/syscall.getpidopt",
                "BenchmarkSyscallUnderSeccomp/cacheable.false", "BenchmarkSyscallUnderSeccomp/cacheable.true",
                "BenchmarkForkUnderSeccomp/seccomp.none", "BenchmarkForkUnderSeccomp/seccomp.cacheable",
                "BenchmarkForkUnderSeccomp/seccomp.uncacheable"}
    directory = evidence / "syscallbench"
    directory.mkdir()
    runtime = "qualification-syscallbench-" + uuid.uuid4().hex
    configuration = {"runtime_name": runtime, "image_name": "benchmarks/syscallbench",
                     "runtime_flags": ["--platform=systrap"],
                     "contents": str(evidence / "syscallbench-contents.json")}
    before = sorted(subprocess.check_output(["docker", "ps", "-aq", "--no-trunc"], text=True, timeout=30).split())
    record = {"status": "INCOMPLETE", "expected_benchmarks": sorted(expected),
              "scope": "Seven existing functional cases, 100 iterations each; no performance comparison."}
    try:
        with prepared_runtime(directory / "runtime-preparation.json", private / "syscallbench", configuration):
            result = invoke("syscallbench-consumer", ["run", "--compilation_mode=opt", "--config=x86_64",
                            "--lockfile_mode=error", "--noannounce_rc", "//test/benchmarks/base:syscallbench_test", "--",
                            "-runtime=" + runtime, "-test.run=^$",
                            "-test.bench=^(BenchmarkSyscallbench|BenchmarkSyscallUnderSeccomp|BenchmarkForkUnderSeccomp)$",
                            "-test.benchtime=100x", "-test.count=1", "-test.timeout=900s"], "command", targets=[])
            output = (evidence / "invocation-syscallbench-consumer/stdout.log").read_text()
            rows = re.findall(r"^(Benchmark\S+?)(?:-\d+)?\s+(\d+)\s+(\S+)\s+ns/op(?:\s|$)", output, re.M)
            record["measurements"] = [{"name": name, "iterations": int(count), "ns/op": float(value)} for name, count, value in rows]
            names = [row["name"] for row in record["measurements"]]
            if result["status"] != "PASS" or len(names) != len(expected) or set(names) != expected:
                raise RuntimeError("The seven syscallbench cases did not all complete")
            if any(row["iterations"] != 100 or not math.isfinite(row["ns/op"]) or row["ns/op"] <= 0 for row in record["measurements"]):
                raise ValueError("Unexpected syscallbench iterations or metrics")
            if re.search(r"^PASS$", output, re.M) is None:
                raise ValueError("The benchmark executable did not report PASS")
        record["status"] = "PASS"
    finally:
        completed = record["status"]
        record["status"] = "FAIL"
        try:
            after = sorted(subprocess.check_output(["docker", "ps", "-aq", "--no-trunc"], text=True, timeout=30).split())
            record["container_inventory"] = {"before": before, "after": after}
            if after != before:
                raise RuntimeError("Syscallbench changed the container inventory")
            record["cleanup"] = "PASS"
            record["status"] = completed
        finally:
            (directory / "consumer.json").write_text(json.dumps(record, indent=2) + "\n")
