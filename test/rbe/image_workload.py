"""Run the two existing AtomicPtr analyzer owners after a comment-only repair."""
import hashlib
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import qualification_report as report


ANALYZERS = ["//pkg/sync/atomicptr:atomicptr_nogo", "//pkg/sync/atomicptr:atomicptr_test_nogo"]


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    for name, expected in config["matched_inputs"].items():
        path = Path(name)
        body = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        if hashlib.sha256(body).hexdigest() != expected:
            raise ValueError("Source input differs: " + name)
    common = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
              "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    formatted = invoke("goimports", ["run", *common, "--run_in_cwd",
                        "@org_golang_x_tools//cmd/goimports", "--", "-format-only", "-d",
                        "pkg/sync/atomicptr/inline_probe.go", "pkg/sync/atomicptr/inline_probe_test.go"],
                       "command", targets=[])
    if formatted["status"] != "PASS" or (evidence / "invocation-goimports/stdout.log").read_bytes():
        raise RuntimeError("Canonical Go formatting failed")
    selected = ["--strategy=TestRunner=local", "--nocache_test_results", "--runs_per_test=1",
                "--flaky_test_attempts=1", "--test_sharding_strategy=disabled", "--test_output=all",
                "--test_timeout=180", "--test_tag_filters=", "--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1"]
    verdict = invoke("atomicptr-nogo", ["test", *common, *selected, *ANALYZERS], "test", targets=ANALYZERS)
    if verdict["status"] != "PASS":
        raise RuntimeError("Native analyzer gate failed")
    executed = verdict["executed"]
    if len(executed) != len(ANALYZERS) or {row["owner"]["label"] for row in executed} != set(ANALYZERS):
        raise ValueError("Unexpected native attempt census")
    for row in executed:
        if (row["status"] != "PASSED" or row["cached_locally"]
                or (row["run"], row["attempt"], row["shard"]) != (1, 1, 1)):
            raise ValueError("Unexpected native attempt: " + str(row))
    _, reports, errors = report.retained_reports(report.manifest_path(evidence / "invocation-atomicptr-nogo/events.jsonl"))
    if errors:
        raise ValueError(str(errors))
    for owner in ANALYZERS:
        selected_reports = [(key, path) for key, path in reports.items() if key[0].owner.label == owner]
        if sorted(key[1] for key, _ in selected_reports) != ["test.log", "test.xml"]:
            raise ValueError("Missing unique native reports: " + owner)
        log = next(path for key, path in selected_reports if key[1] == "test.log")
        xml = next(path for key, path in selected_reports if key[1] == "test.xml")
        cases = list(ET.parse(xml).getroot().iter("testcase"))
        if len(cases) != 1 or any(list(case) for case in cases) or not all(cpu + "...PASS" in log.read_text() for cpu in ("amd64", "arm64")):
            raise ValueError("Missing both-CPU positive analyzer results: " + owner)
    report.write_json(evidence / "atomicptr-analyzers.json", {"status": "PASS", "source": config["source"], "executed": executed})
