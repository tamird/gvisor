"""Qualify bounded sample ports and TPU timeout request semantics."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import qualification_report as report


UNIT = "//tools/tpucheckpoint/tpucontrol:tpucontrol_test"
BINARIES = ["//pkg/tcpip/sample/tun_tcp_connect:tun_tcp_connect", "//pkg/tcpip/sample/tun_tcp_echo:tun_tcp_echo", "//tools/tpucheckpoint:tpucheckpoint"]
ANALYZERS = [target + "_nogo" for target in BINARIES] + ["//tools/tpucheckpoint/tpucontrol:tpucontrol_nogo", UNIT + "_nogo"]
UNIT_CASES = {"TestControlSuccess/" + operation + "/" + case
              for operation in ("checkpoint", "restore", "get_state")
              for case in ("positive", "maximum", "zero", "negative", "minimum")}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["targets"] != [UNIT, *ANALYZERS]:
        raise ValueError(f"Unexpected correction owner selection: {config['targets']}")
    record = {"source": config["source"], "product": config["product"],
              "status": "INCOMPLETE", "checks": [], "errors": [],
              "architecture_scope": "AMD64 TPU request tests; both-CPU builds and Nogo"}
    try:
        for path, expected in config["matched_inputs"].items():
            actual = subprocess.check_output(["git", "show", config["source"] + ":" + path])
            if hashlib.sha256(actual).hexdigest() != expected:
                raise ValueError(f"Frozen source input differs: {path}")
        options = ["--compilation_mode=opt", "--lockfile_mode=error", *config["consumer_options"],
                   "--remote_download_outputs=all", "--noannounce_rc", "--keep_going"]
        formatted = invoke("goimports", ["run", "--config=x86_64", *options, "--run_in_cwd",
                           "@org_golang_x_tools//cmd/goimports", "--", "-format-only", "-d",
                           "pkg/tcpip/sample/tun_tcp_connect/main.go", "pkg/tcpip/sample/tun_tcp_echo/main.go",
                           "tools/tpucheckpoint/tpucheckpoint.go", "tools/tpucheckpoint/tpucontrol/control.go",
                           "tools/tpucheckpoint/tpucontrol/control_test.go"],
                           "command", targets=[])
        record["checks"].append({"name": "goimports", "result": formatted})
        for architecture in ("x86_64", "aarch64"):
            result = invoke("build-" + architecture, ["build", "--config=" + architecture, *options, *BINARIES], "build", targets=BINARIES)
            record["checks"].append({"name": "build-" + architecture, "result": result})
        testing = ["--config=x86_64", *options, "--strategy=TestRunner=local", "--nocache_test_results",
                   "--runs_per_test=1", "--flaky_test_attempts=1", "--test_output=all",
                   "--test_sharding_strategy=disabled", "--test_timeout=600", "--test_tag_filters="]
        phases = [("tpu-unit", [UNIT], ["--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1"]),
                  ("corrections-nogo", ANALYZERS, [])]
        # Finish every native test phase before interpreting reports, so a
        # reporting error does not erase independent architecture/test evidence.
        for name, owners, extra in phases:
            result = invoke(name, ["test", *testing, *extra, *owners], "test", targets=owners)
            record["checks"].append({"name": name, "result": result, "reports": []})
        for check in record["checks"]:
            result = check["result"]
            if result["status"] != "PASS":
                record["errors"].append(f"{check['name']}: native verdict {result['status']}")
        if (evidence / "invocation-goimports/stdout.log").read_bytes():
            record["errors"].append("Canonical Go import formatting differs")
        seen_paths: set[Path] = set()
        for name, owners, _ in phases:
            check = next(row for row in record["checks"] if row["name"] == name)
            result = check["result"]
            try:
                counts = result["case_counts"]
                if counts["failed"] or counts["skipped"] or not counts["passed"]:
                    raise ValueError(f"Expected positive cases without failures/skips: {counts}")
                attempts = result["executed"]
                if len(attempts) != len(owners) or {a["owner"]["label"] for a in attempts} != set(owners):
                    raise ValueError(f"Executed owners differ: {attempts}")
                expected_attempts = set()
                for attempt in attempts:
                    if (attempt["run"], attempt["shard"], attempt["attempt"]) != (1, 1, 1) or attempt["cached_locally"] or attempt["execution_info"].get("cachedRemotely", False) or attempt["execution_info"].get("strategy") != "local":
                        raise ValueError(f"Expected one uncached hosted-local attempt: {attempt}")
                    expected_attempts.add((attempt["owner"]["label"], attempt["owner"]["configuration"], 1, 1, 1))
                _, reports, errors = report.retained_reports(report.manifest_path(evidence / ("invocation-" + name) / "events.jsonl"))
                actual_attempts = {(a.owner.label, a.owner.configuration, a.run, a.shard, a.attempt)
                                   for (a, kind) in reports if kind == "test.xml"}
                if errors or actual_attempts != expected_attempts or len(reports) != 2 * len(owners):
                    raise ValueError(f"Missing, aliased or unexpected retained reports: {errors}, {actual_attempts}")
                for (attempt, kind), path in reports.items():
                    if path in seen_paths:
                        raise ValueError(f"Repeated retained report path: {path}")
                    seen_paths.add(path)
                    if kind != "test.xml":
                        continue
                    log_path = reports[(attempt, "test.log")]
                    log = log_path.read_text(errors="strict")
                    cases = list(ET.parse(path).getroot().iter("testcase"))
                    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
                        raise ValueError("XML contains failed, skipped or errored cases")
                    names = [case.attrib["name"] for case in cases]
                    details = {"owner": attempt.owner.label, "configuration": attempt.owner.configuration,
                               "xml": report.file_record(path), "log": report.file_record(log_path), "names": names}
                    if name == "tpu-unit":
                        if not UNIT_CASES <= set(names):
                            raise ValueError(f"Missing operation-specific wire cases: {UNIT_CASES - set(names)}")
                    elif name == "corrections-nogo":
                        if len(cases) != 1 or any(marker not in log for marker in ("amd64...PASS", "arm64...PASS")):
                            raise ValueError("Missing one positive Nogo wrapper or both architecture markers")
                    check["reports"].append(details)
            except (KeyError, OSError, ValueError, TypeError, ET.ParseError) as error:
                record["errors"].append(f"{name}: {error}")
        if record["errors"]:
            raise RuntimeError("; ".join(record["errors"]))
        record["status"] = "PASS"
    except Exception as error:
        record["status"] = "FAIL"
        record["error"] = str(error)
        raise
    finally:
        report.write_json(evidence / "parseuint-corrections.json", record)
