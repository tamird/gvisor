"""Run configured Go suites and analyzers; check their native reports."""
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    required = config["required_cases"]
    analyzers = set(config["analyzers"])
    targets = config["targets"]
    if set(required) & analyzers or set(targets) != set(required) | analyzers:
        raise ValueError("Each target must have either required test cases or analyzer results")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    formatted = invoke("goimports", ["run", *options, "--run_in_cwd",
                       "@org_golang_x_tools//cmd/goimports", "--", "-format-only", "-d",
                       *config["format_files"]], "command", targets=[])
    if formatted["status"] != "PASS" or (evidence / "invocation-goimports/stdout.log").read_bytes():
        raise ValueError("Canonical goimports reported a formatting difference")

    result = invoke("go-checks", ["test", *options, "--keep_going", "--strategy=TestRunner=remote",
                    "--modify_execution_info=^TestRunner$=+no-remote-cache",
                    "--nocache_test_results", "--runs_per_test=1", "--flaky_test_attempts=1",
                    "--test_output=all", "--test_env=GO_TEST_WRAP=1",
                    "--test_env=GO_TEST_WRAP_TESTV=1", *targets], "test", targets=targets)
    if result["status"] != "PASS" or result["case_counts"]["skipped"]:
        raise RuntimeError("Not all requested checks passed; see the native test verdict")
    executed = result["executed"]
    if len(executed) != len(targets) or {a["owner"]["label"] for a in executed} != set(targets):
        raise ValueError("Missing or repeated test attempts")
    for attempt in executed:
        if (attempt["status"] != "PASSED" or attempt["cached_locally"]
                or attempt["execution_info"].get("cachedRemotely", False)
                or attempt["execution_info"].get("strategy") != "remote"
                or (attempt["run"], attempt["shard"], attempt["attempt"]) != (1, 1, 1)):
            raise ValueError(f"Expected one uncached remote test attempt: {attempt}")
    manifest = report.manifest_path(evidence / "invocation-go-checks/events.jsonl")
    _, reports, errors = report.retained_reports(manifest)
    if errors:
        raise ValueError(f"Invalid retained test reports: {errors}")
    for owner in targets:
        files = {kind: path for (attempt, kind), path in reports.items() if attempt.owner.label == owner}
        if set(files) != {"test.xml", "test.log"}:
            raise ValueError(f"Missing report for {owner}")
        cases = ET.parse(files["test.xml"]).getroot().findall(".//testcase")
        if not cases or any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
            raise ValueError(f"Invalid positive XML census for {owner}")
        if owner in analyzers:
            log = files["test.log"].read_text()
            if not all(f"{cpu}...PASS" in log for cpu in ("amd64", "arm64")):
                raise ValueError(f"Missing an analyzer architecture for {owner}")
        else:
            names = {case.attrib["name"] for case in cases}
            if missing := set(required[owner]) - names:
                raise ValueError(f"Required cases did not execute for {owner}: {sorted(missing)}")
