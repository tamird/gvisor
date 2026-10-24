"""Qualify the real TCP proxy CLI and native forwarding path."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import qualification_report as report


UNIT = "//test/benchmarks/tcp:tcp_proxy_cli_qualification"
BINARIES = ["//test/benchmarks/tcp:tcp_proxy"]
ANALYZERS = [BINARIES[0] + "_nogo"]
UNIT_CASES = {"test_listen_overflow_before_setup", "test_forward_overflow_before_setup", "test_valid_forward_reaches_setup", "test_native_forwarding_hex_listen"}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["targets"] != [UNIT, *ANALYZERS]:
        raise ValueError(f"Unexpected correction owner selection: {config['targets']}")
    record = {"source": config["source"], "product": config["product"],
              "status": "INCOMPLETE", "checks": [], "errors": [],
              "architecture_scope": "AMD64 actual proxy CLI/forwarding; both-CPU builds and Nogo"}
    try:
        for path, expected in config["matched_inputs"].items():
            actual = subprocess.check_output(["git", "show", config["source"] + ":" + path])
            if hashlib.sha256(actual).hexdigest() != expected:
                raise ValueError(f"Frozen source input differs: {path}")
        options = ["--compilation_mode=opt", "--lockfile_mode=error", *config["consumer_options"],
                   "--remote_download_outputs=all", "--noannounce_rc", "--keep_going"]
        formatted = invoke("goimports", ["run", "--config=x86_64", *options, "--run_in_cwd",
                           "@org_golang_x_tools//cmd/goimports", "--", "-format-only", "-d",
                           "test/benchmarks/tcp/tcp_proxy.go"],
                           "command", targets=[])
        record["checks"].append({"name": "goimports", "result": formatted})
        for architecture in ("x86_64", "aarch64"):
            result = invoke("build-" + architecture, ["build", "--config=" + architecture, *options, *BINARIES], "build", targets=BINARIES)
            record["checks"].append({"name": "build-" + architecture, "result": result})
        queried = invoke("binary-path", ["cquery", "--config=x86_64", *options,
                          "--output=files", BINARIES[0]], "command", targets=[])
        record["checks"].append({"name": "binary-path", "result": queried})
        testing = ["--config=x86_64", *options, "--strategy=TestRunner=local", "--nocache_test_results",
                   "--runs_per_test=1", "--flaky_test_attempts=1", "--test_output=all",
                   "--test_sharding_strategy=disabled", "--test_timeout=600", "--test_tag_filters="]
        phases = [("proxy-cli", [UNIT], []),
                  ("proxy-nogo", ANALYZERS, [])]
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
                    if name == "proxy-cli":
                        if len(names) != len(UNIT_CASES) or set(names) != UNIT_CASES:
                            raise ValueError(f"Missing actual proxy CLI cases: {UNIT_CASES - set(names)}")
                        markers = [line.removeprefix("TCP_PROXY_CLI_OBSERVATIONS=") for line in log.splitlines() if line.startswith("TCP_PROXY_CLI_OBSERVATIONS=")]
                        if len(markers) != 1:
                            raise ValueError("Missing or repeated actual proxy observations")
                        observed = json.loads(markers[0])
                        paths = (evidence / "invocation-binary-path/stdout.log").read_text().splitlines()
                        if len(paths) != 1:
                            raise ValueError(f"Expected one declared proxy output: {paths}")
                        binary = Path(paths[0]).resolve(strict=True)
                        if not os.access(binary, os.X_OK) or observed["binary"] != report.file_record(binary):
                            raise ValueError("Executed proxy does not match the declared build output")
                        rows = observed["observations"]
                        if not observed["success"] or len(rows) != 4 or {r["case"] for r in rows} != UNIT_CASES:
                            raise ValueError("Proxy observation case set/status differs")
                        for row in rows:
                            case = row["case"]
                            if case.endswith("overflow_before_setup"):
                                prefix = "invalid listen port:" if case.startswith("test_listen") else "invalid forwarding target:"
                                if row["exit"] != 1 or prefix not in row["stderr"] or row["profile_created"] or any(v in row["stderr"] for v in ("netstack error:", "connect failed retrying")):
                                    raise ValueError(f"Early rejection boundary failed: {row}")
                            elif case == "test_valid_forward_reaches_setup":
                                if row["exit"] != 1 or "netstack error: failed to find interface: qualification-missing" not in row["stderr"]:
                                    raise ValueError(f"Valid target did not reach device setup: {row}")
                            else:
                                payload = bytes(range(256)) * 257
                                response = b"response:" + payload[::-1]
                                if row["exit"] != 0 or row.get("cleanup_killed") or row["sent_bytes"] != len(payload) or row["returned_bytes"] != len(response) or row["payload_sha256"] != hashlib.sha256(payload).hexdigest() or row["response_sha256"] != hashlib.sha256(response).hexdigest() or "-port=" + hex(row["listen_port"]) not in row["argv"]:
                                    raise ValueError(f"Native hex-port forwarding failed: {row}")
                        details["observations"] = observed
                        details["declared_binary"] = {"path": str(binary), **report.file_record(binary)}
                    elif name == "proxy-nogo":
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
        report.write_json(evidence / "tcp-proxy-cli.json", record)
