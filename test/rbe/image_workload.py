"""Check proctor formatting, its existing unit tests, and both-CPU Nogo."""
import json
import os
from pathlib import Path
import shlex

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    unit_owner = "//test/runtimes/proctor/lib:lib_test"
    nogo_owner = "//test/runtimes/proctor/lib:lib_nogo"
    if config["targets"] != [unit_owner, nogo_owner]:
        raise ValueError(f"Unexpected proctor check targets: {config['targets']}")
    record = {"source": config["source"], "status": "INCOMPLETE", "checks": []}
    try:
        # Reuse the canonical check with the declared SDK on PATH. This checks
        # tracked Go files without rewriting them or installing a host tool.
        formatter = shlex.join([
            "bash", "-c",
            'set -eu; export PATH="$(dirname "$1"):$PATH"; '
            'cd "$BUILD_WORKSPACE_DIRECTORY"; exec bash tools/lint.sh gofmt',
            "--",
        ])
        formatted = invoke("gofmt", [
            "run", "--lockfile_mode=error", "--noannounce_rc",
            "--run_under=" + formatter, "@io_bazel_rules_go//go:go",
        ], "command", targets=[])
        record["gofmt"] = formatted["status"]
        if formatted["status"] != "PASS":
            record["status"] = "FAIL"
            raise RuntimeError("Canonical Go formatting failed")
        options = [
            "--compilation_mode=opt", "--config=x86_64", "--lockfile_mode=error",
            "--remote_download_outputs=all", "--noannounce_rc",
            "--build_event_json_file_path_conversion=false", "--keep_going",
            "--strategy=TestRunner=local", "--nocache_test_results",
            "--runs_per_test=1", "--flaky_test_attempts=1", "--test_output=all",
        ]
        # lib_test owns TestSearch and TestSearchEmptyDir. The single native
        # lib_nogo shell checks both arch_transition dependencies; do not run
        # its ARM64 host binary on this AMD64 worker.
        for name, owner, count in (
                ("proctor-unit", unit_owner, 2), ("proctor-nogo", nogo_owner, 1)):
            reporting = ["--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1"] if owner == unit_owner else []
            result = invoke(name, ["test", *options, *reporting, owner], "test", targets=[owner])
            record["checks"].append({
                "name": name, "owner": owner, "status": result["status"],
                "case_counts": result["case_counts"], "executed": result["executed"],
            })
            expected = {"failed": 0, "passed": count, "skipped": 0}
            if result["status"] != "PASS" or result["case_counts"] != expected:
                record["status"] = "FAIL"
                raise RuntimeError(f"{owner}: got {result['status']} {result['case_counts']}; want PASS {expected}")
            match result["executed"]:
                case [attempt]:
                    if attempt["cached_locally"] or attempt["execution_info"].get("cachedRemotely", False):
                        record["status"] = "FAIL"
                        raise RuntimeError(f"Expected an uncached {owner} attempt")
                case attempts:
                    record["status"] = "FAIL"
                    raise RuntimeError(f"Expected one {owner} attempt, got {attempts}")
        record["status"] = "PASS"
    finally:
        report.write_json(evidence / "php-proctor-checks.json", record)
