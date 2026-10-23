"""Run the existing pidfd consumer against its inspected source-built image."""
from dataclasses import asdict
import json
from pathlib import Path

import qualification_report as report

from image_runtime import prepared_runtime
from pidfd_tap import reconcile


def run(invoke, evidence: Path, private: Path) -> None:
    scripts = Path(__file__).parent
    python = ["run", "--lockfile_mode=error", "--noannounce_rc",
              "@rules_python//python/bin:python", "--"]
    contents_path = evidence / "pidfd-contents.json"
    inspected = invoke("pidfd-image-closure", [*python, str(scripts / "pidfd_image_inspect.py"),
                        str(contents_path)], "command", targets=[])
    if inspected["status"] != "PASS":
        raise RuntimeError("Pidfd archive/source/runtime closure verification failed")
    contents = report.read_json(contents_path)
    programs = next([item["name"] for item in row["static_programs"]]
                    for row in contents["architectures"] if row["architecture"] == "amd64")
    configuration = {
        "runtime_name": "qualification-pidfd-cgroupv2",
        "image_name": "basic/pidfd-tests",
        "runtime_flags": ["--platform=systrap", "--in-sandbox-cgroup=v2"],
        "contents": str(contents_path),
    }
    with prepared_runtime(evidence / "runtime-preparation.json", private / "runtime", configuration):
        targets = ["//test/e2e:integration_runtime_test"]
        options = ["--compilation_mode=opt", "--lockfile_mode=error", "--noannounce_rc",
                   "--remote_download_outputs=all", "--build_event_json_file_path_conversion=false",
                   "--config=x86_64", "--runs_per_test=1", "--flaky_test_attempts=1",
                   "--nocache_test_results", "--test_env=GO_TEST_WRAP_TESTV=1",
                   "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled",
                   "--strategy=TestRunner=local", "--local_test_jobs=1", "--test_timeout=900",
                   "--test_arg=-runtime=qualification-pidfd",
                   "--test_filter=^TestPIDFDSelftests$", "--test_arg=-test.run=^TestPIDFDSelftests$"]
        analysis = invoke("pidfd-consumer-analysis", ["test", *options, "--nobuild", *targets],
                          "analysis", targets=targets)
        if analysis["status"] != "PASS":
            raise RuntimeError("Existing pidfd consumer analysis failed")
        tested = invoke("pidfd-consumer", ["test", *options, *targets], "test",
                        (evidence / "invocation-pidfd-consumer-analysis/events.jsonl",), targets=targets)
        destination = evidence / "pidfd-selftest-results.json"
        result = {"status": "INCOMPLETE", "expected_programs": programs}
        try:
            manifest = report.manifest_path(evidence / "invocation-pidfd-consumer/events.jsonl")
            _, retained, errors = report.retained_reports(manifest)
            logs = [(attempt, path) for (attempt, name), path in retained.items() if name == "test.log"]
            if errors or len(logs) != 1:
                raise ValueError(f"Expected one authenticated consumer log: {errors}; {logs}")
            attempt, path = logs[0]
            result.update(attempt=asdict(attempt), log=report.file_record(path))
            result.update(reconcile(path.read_text(), programs))
            if tested["status"] != "PASS" or tested["case_counts"].get("skipped", 0):
                raise RuntimeError("The required pidfd consumer failed or skipped")
        except (OSError, ValueError, RuntimeError) as error:
            result.update(status="FAIL", error=str(error))
            raise
        finally:
            report.write_json(destination, result)
