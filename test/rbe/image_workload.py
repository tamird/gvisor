"""Run current public NVIDIA checksum expectations without installing drivers."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import qualification_report as report


OWNER = "//pkg/sentry/devices/nvproxy:nvproxy_driver_parity_test"
EMULATOR = "//tools/bazeldefs:qemu_aarch64"
VERSIONS = ("580.173.10", "620.51.03")
CASES = {"TestDriverChecksums", *("TestDriverChecksums/" + version for version in VERSIONS)}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["targets"] != [OWNER] or config["versions"] != list(VERSIONS):
        raise ValueError("Unexpected checksum selection")
    record = {"source": config["source"], "product": config["product"],
              "status": "INCOMPLETE", "checks": [], "errors": [],
              "scope": "AMD64 native and ARM64 under declared QEMU; installer HTTP/checksum only; no ABI, installation or GPU execution"}
    try:
        for path, expected in config["matched_inputs"].items():
            actual = subprocess.check_output(["git", "show", config["source"] + ":" + path])
            if hashlib.sha256(actual).hexdigest() != expected:
                raise ValueError(f"Frozen source input differs: {path}")
        options = ["--compilation_mode=opt", "--lockfile_mode=error", *config["consumer_options"],
                   "--remote_download_outputs=all", "--noannounce_rc", "--keep_going"]
        for name, command in (
            ("emulator-build", ["build", "--config=x86_64", *options, EMULATOR]),
            ("emulator-path", ["cquery", "--config=x86_64", *options, "--output=files", EMULATOR]),
        ):
            result = invoke(name, command, "build" if name.endswith("build") else "command",
                            targets=[EMULATOR] if name.endswith("build") else [])
            record["checks"].append({"name": name, "result": result})
        paths = (evidence / "invocation-emulator-path/stdout.log").read_text().splitlines()
        if len(paths) != 1:
            raise ValueError(f"Expected one declared QEMU executable: {paths}")
        relative_emulator = Path(paths[0])
        if relative_emulator.is_absolute() or relative_emulator.parts[0] != "external" or ".." in relative_emulator.parts:
            raise ValueError("Expected a declared external QEMU source file")
        emulator = (output_root / relative_emulator).resolve(strict=True)
        if not os.access(emulator, os.X_OK):
            raise ValueError("Declared emulator is not executable")
        record["emulator"] = {"path": str(emulator), "cquery_path": paths[0],
                              "output_base": str(output_root), **report.file_record(emulator)}
        testing = [*options, "--strategy=TestRunner=local", "--nocache_test_results",
                   "--runs_per_test=1", "--flaky_test_attempts=1", "--test_output=all",
                   "--test_sharding_strategy=disabled", "--test_timeout=900", "--test_tag_filters=",
                   "--test_filter=^TestDriverChecksums$/^(580[.]173[.]10|620[.]51[.]03)$", "--test_arg=-test.v"]
        phases = [("checksums-amd64", "x86_64", []),
                  ("checksums-arm64", "aarch64", ["--run_under=" + str(emulator) + " -cpu neoverse-n1"])]
        # Preserve both native attempts before interpreting either result.
        for name, architecture, extra in phases:
            result = invoke(name, ["test", "--config=" + architecture, *testing, *extra, OWNER],
                            "test", targets=[OWNER])
            record["checks"].append({"name": name, "architecture": architecture,
                                      "result": result, "reports": []})
        for check in record["checks"]:
            if check["result"]["status"] != "PASS":
                record["errors"].append(f"{check['name']}: native verdict {check['result']['status']}")
        seen_paths: set[Path] = set()
        for name, _, _ in phases:
            check = next(row for row in record["checks"] if row["name"] == name)
            try:
                result = check["result"]
                attempts = result["executed"]
                if len(attempts) != 1 or attempts[0]["owner"]["label"] != OWNER:
                    raise ValueError(f"Expected the actual checksum owner: {attempts}")
                attempt = attempts[0]
                if (attempt["run"], attempt["shard"], attempt["attempt"]) != (1, 1, 1) or attempt["cached_locally"] or attempt["execution_info"].get("cachedRemotely", False) or attempt["execution_info"].get("strategy") != "local":
                    raise ValueError(f"Expected one uncached hosted-local attempt: {attempt}")
                _, reports, errors = report.retained_reports(report.manifest_path(evidence / ("invocation-" + name) / "events.jsonl"))
                identities = {(a.owner.label, a.owner.configuration, a.run, a.shard, a.attempt)
                              for a, kind in reports if kind == "test.xml"}
                expected = {(OWNER, attempt["owner"]["configuration"], 1, 1, 1)}
                if errors or identities != expected or len(reports) != 2:
                    raise ValueError(f"Missing or unexpected retained reports: {errors}, {identities}")
                for (identity, kind), path in reports.items():
                    if path in seen_paths:
                        raise ValueError(f"Repeated retained report path: {path}")
                    seen_paths.add(path)
                    if kind != "test.xml":
                        continue
                    log_path = reports[(identity, "test.log")]
                    log = log_path.read_text(errors="strict")
                    cases = list(ET.parse(path).getroot().iter("testcase"))
                    names = [case.attrib["name"] for case in cases]
                    details = {"owner": OWNER, "configuration": identity.owner.configuration,
                               "xml": report.file_record(path), "log": report.file_record(log_path),
                               "cases": [{"name": case.attrib["name"], "status": next((tag for tag in ("failure", "error", "skipped") if case.find(tag) is not None), "PASS")} for case in cases]}
                    check["reports"].append(details)
                    if len(names) != len(CASES) or set(names) != CASES:
                        raise ValueError(f"Checksum case set differs: {names}")
                    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
                        raise ValueError("Native XML contains failed, skipped or errored checksum cases")
                    required = [
                        'NVIDIA .run installer does not exist for driver "580.173.10" arch: "amd64"',
                        'NVIDIA .run installer is unavailable at the public URL for driver "580.173.10" arch: "arm64"',
                        'NVIDIA .run installer does not exist for driver "620.51.03" arch: "amd64"',
                        'Checksum for driver "620.51.03" arch: "arm64": "7c7f4e71ec27a6c050ed3d074eda4bd3c10a0c2d3f330cd1bf9485e5265a569e"',
                    ]
                    if any(marker not in log for marker in required):
                        raise ValueError("Missing actual public installer/checksum result")
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
        report.write_json(evidence / "nvproxy-public-checksums.json", record)
