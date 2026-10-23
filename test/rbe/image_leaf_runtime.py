"""Run the existing tmpfile test and four syscallbench modes on scratch images."""
import json
from pathlib import Path
import re

from image_runtime import prepared_runtime


def run(invoke, evidence: Path, private: Path) -> None:
    expected = {"BenchmarkSyscallbench/syscall.getpid", "BenchmarkSyscallbench/syscall.getpidopt",
                "BenchmarkSyscallUnderSeccomp/cacheable.false", "BenchmarkSyscallUnderSeccomp/cacheable.true"}
    for image, component in (("tmpfile", "basic/tmpfile"), ("syscallbench", "benchmarks/syscallbench")):
        directory = evidence / image
        directory.mkdir()
        configuration = {"runtime_name": "qualification-leaf", "image_name": component,
                         "runtime_flags": ["--platform=systrap"],
                         "contents": str(evidence / (image + "-contents.json"))}
        with prepared_runtime(directory / "runtime-preparation.json", private / image, configuration):
            if image == "tmpfile":
                targets = ["//test/e2e:integration_test"]
                options = ["--compilation_mode=opt", "--lockfile_mode=error", "--noannounce_rc",
                           "--remote_download_outputs=all", "--build_event_json_file_path_conversion=false",
                           "--config=x86_64", "--runs_per_test=1", "--flaky_test_attempts=1",
                           "--nocache_test_results", "--test_env=GO_TEST_WRAP_TESTV=1",
                           "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled",
                           "--strategy=TestRunner=local", "--local_test_jobs=1", "--test_timeout=900",
                           "--test_arg=-runtime=qualification-leaf", "--test_filter=^TestTmpFile$",
                           "--test_arg=-test.run=^TestTmpFile$"]
                analysis = invoke("tmpfile-consumer-analysis", ["test", *options, "--nobuild", *targets],
                                  "analysis", targets=targets)
                if analysis["status"] != "PASS":
                    raise RuntimeError("Existing tmpfile consumer analysis failed")
                result = invoke("tmpfile-consumer", ["test", *options, *targets], "test",
                                (evidence / "invocation-tmpfile-consumer-analysis/events.jsonl",), targets=targets)
                if result["status"] != "PASS" or result["case_counts"].get("skipped", 0):
                    raise RuntimeError("Required tmpfile consumer failed or skipped")
            else:
                result = invoke("syscallbench-consumer", ["run", "--compilation_mode=opt", "--config=x86_64",
                                "--lockfile_mode=error", "--noannounce_rc", "//test/benchmarks/base:syscallbench_test", "--",
                                "-runtime=qualification-leaf", "-test.run=^$",
                                "-test.bench=^(BenchmarkSyscallbench|BenchmarkSyscallUnderSeccomp)$",
                                "-test.benchtime=100x", "-test.count=1"], "command", targets=[])
                output = (evidence / "invocation-syscallbench-consumer/stdout.log").read_text()
                names = re.findall(r"^(Benchmark\S+?)(?:-\d+)?\s+100\s+", output, re.MULTILINE)
                record = {"status": "PASS" if result["status"] == "PASS" and len(names) == len(expected) and set(names) == expected else "FAIL",
                          "expected_benchmarks": sorted(expected), "executed_benchmarks": names,
                          "scope": "Existing four functional modes,100 iterations each; no performance comparison."}
                (directory / "consumer.json").write_text(json.dumps(record, indent=2) + "\n")
                if record["status"] != "PASS":
                    raise RuntimeError("Existing syscallbench modes did not all execute")
