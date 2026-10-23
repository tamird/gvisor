"""Exercise the actual HTTP readiness cancellation and cleanup contract."""
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import tarfile
import time
import urllib.request
import xml.etree.ElementTree as ET

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    owner = "//test/benchmarks/harness:http_probe_cancellation_test"
    if config["targets"] != [owner]:
        raise ValueError("Unexpected HTTP probe owner")
    records = {"source": config["source"], "status": "INCOMPLETE", "commands": []}
    tag = "gvisor.dev/images/basic/busybox:latest"
    original = None
    tagged = False

    def remaining() -> float:
        seconds = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic() - 60
        if seconds <= 0:
            raise TimeoutError("HTTP probe qualification exhausted its deadline")
        return seconds

    def command(arguments: list[str]) -> str:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=min(60, remaining()))
        records["commands"].append({"arguments": arguments, "native_exit": result.returncode,
                                    "stdout": result.stdout, "stderr": result.stderr})
        result.check_returncode()
        return result.stdout.strip()

    def fetch(expected: dict, destination: Path) -> None:
        request = urllib.request.Request("https://app.buildbuddy.io/api/v1/GetFile",
            data=json.dumps({"uri": expected["uri"]}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with urllib.request.urlopen(request, timeout=min(60, remaining())) as response:
            data = response.read(expected["bytes"] + 1)
        if len(data) != expected["bytes"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise ValueError("Retained HTTP fixture identity differs")
        destination.write_bytes(data)

    try:
        supplier = config["fixture_supplier"]
        retained = evidence / "retained-fixture-evidence.tar.gz"
        fetch(supplier, retained)
        with tarfile.open(retained) as bundle:
            entries = [entry for entry in bundle if entry.isfile()]
            if len({entry.name for entry in entries}) != len(entries):
                raise ValueError("Duplicate retained fixture evidence member")
            members = {entry.name: bundle.extractfile(entry).read() for entry in entries}
        previous = json.loads(members["invocation-busybox-x86_64/verdict.json"])
        if previous["source"] != supplier["source"] or previous["status"] != "PASS" or previous["native_exit"] != 0:
            raise ValueError("Retained fixture source/build failed")
        events = evidence / "retained-fixture-events.jsonl"
        events.write_bytes(members["invocation-busybox-x86_64/events.jsonl"])
        owners = {report.Owner(row["label"], row["configuration"]) for row in previous["expected"]
                  if row["label"] == "//images/basic:busybox_archive"}
        outputs = report.advertised_build_outputs(events, {(owner, "default") for owner in owners})
        fixture = config["fixture"]
        if len(owners) != 1 or len(outputs) != 1 or any(outputs[0][key] != fixture[key] for key in ("sha256", "bytes")):
            raise ValueError("Fixture differs from the retained configured default output")
        records["fixture_supplier"] = supplier
        records["configured_output"] = outputs[0]
        archive = private / "busybox.tar"
        fetch(fixture, archive)
        records["fixture"] = fixture
        records["containers_before"] = command(["docker", "ps", "--all", "--quiet", "--no-trunc"]).splitlines()
        tags = command(["docker", "image", "ls", "--format={{.Repository}}:{{.Tag}}"]).splitlines()
        if tag in tags:
            original = command(["docker", "image", "inspect", tag, "--format={{.Id}}"])
        tagged = True
        command(["docker", "load", "--input", str(archive)])
        image = json.loads(command(["docker", "image", "inspect", "sha256:" + fixture["config_sha256"]]))
        match image:
            case [{"Architecture": "amd64", "Os": "linux", "Id": identifier}] if identifier == "sha256:" + fixture["config_sha256"]:
                pass
            case _:
                raise ValueError("Loaded fixture image/platform differs")
        command(["docker", "tag", identifier, tag])
        records["image"] = image[0]
        formatter = shlex.join(["bash", "-c", 'set -eu; export PATH="$(dirname "$1"):$PATH"; cd "$BUILD_WORKSPACE_DIRECTORY"; exec bash tools/lint.sh gofmt', "--"])
        formatted = invoke("gofmt", ["run", "--lockfile_mode=error", "--noannounce_rc",
                           "--run_under=" + formatter, "@io_bazel_rules_go//go:go"], "command", targets=[])
        if formatted["status"] != "PASS":
            raise RuntimeError("Canonical Go formatting failed")
        options = ["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
                   "--noannounce_rc", "--remote_download_outputs=all", "--build_event_json_file_path_conversion=false",
                   "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                   "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled",
                   "--test_env=GO_TEST_WRAP_TESTV=1", "--strategy=TestRunner=local", "--local_test_jobs=1",
                   "--test_timeout=120", "--test_filter=^TestHTTPProbeCancellation$", "--test_arg=-test.v=test2json"]
        analyzed = invoke("http-probe-analysis", ["test", *options, "--nobuild", owner], "analysis", targets=[owner])
        if analyzed["status"] != "PASS":
            raise RuntimeError("HTTP probe analysis failed")
        tested = invoke("http-probe", ["test", *options, owner], "test",
                        (evidence / "invocation-http-probe-analysis/events.jsonl",), targets=[owner])
        records["test"] = tested
        owned_names = {}
        records["owned_cleanup"] = []
        try:
            _, retained_reports, errors = report.retained_reports(
                report.manifest_path(evidence / "invocation-http-probe/events.jsonl"))
            if errors:
                raise ValueError("Cannot authenticate HTTP probe reports: " + str(errors))
            logs = [(attempt, path) for (attempt, name), path in retained_reports.items() if name == "test.log"]
            match logs:
                case [(attempt, log)] if attempt.owner.label == owner:
                    records["test_log"] = report.file_record(log)
                case _:
                    raise ValueError("Expected exactly one selected HTTP probe log")
            text = log.read_text().replace("\x16", "")
            for line in text.splitlines():
                if "HTTP_PROBE_OWNED " not in line:
                    continue
                match line.split("HTTP_PROBE_OWNED ", 1)[1].split():
                    case [kind, name] if kind in ("server", "probe") and re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                        if kind in owned_names:
                            raise ValueError("Duplicate allocated HTTP fixture name: " + kind)
                        owned_names[kind] = name
                    case _:
                        raise ValueError("Malformed allocated HTTP fixture name")
            records["owned_names"] = owned_names
            xmls = [(attempt, path) for (attempt, name), path in retained_reports.items() if name == "test.xml"]
            match xmls:
                case [(attempt, xml)] if attempt.owner.label == owner:
                    records["xml_names"] = [case.get("name") for case in ET.parse(xml).getroot().iter("testcase")]
                    if records["xml_names"] != ["TestHTTPProbeCancellation"]:
                        raise ValueError("HTTP probe executed a different test case")
                case _:
                    raise ValueError("Expected exactly one selected HTTP probe XML")
            results = [json.loads(line.split("HTTP_PROBE_RESULT ", 1)[1])
                       for line in log.read_text().replace("\x16", "").splitlines() if "HTTP_PROBE_RESULT " in line]
            match results:
                case [result] if all(re.fullmatch(r"[A-Za-z0-9_.-]+", result.get(key, "")) for key in ("server", "probe")):
                    records["caller_observation"] = result
                case _:
                    raise ValueError("Expected one actual HTTP caller result with exact owned names")
            if owned_names != {key: result[key] for key in ("server", "probe")}:
                raise ValueError("HTTP result differs from actual allocated fixture names")
            if (tested["status"] != "PASS" or tested["case_counts"] != {"failed": 0, "passed": 1, "skipped": 0}
                    or result["status"] != "PASS" or result["running_before_cancel"] is not True
                    or result["caller_error"] == "<nil>" or result["after_cancel_inventory"].strip()):
                raise RuntimeError("Actual HTTP readiness cancellation/cleanup failed")
            match tested["executed"]:
                case [attempt] if not attempt["cached_locally"] and not attempt["execution_info"].get("cachedRemotely", False):
                    pass
                case _:
                    raise ValueError("HTTP probe must execute once without a cached test result")
        finally:
            # The native process has ended. This cannot race its worker or turn
            # a failed pre-fallback assertion into a successful caller result.
            cleanup_errors = []
            for name in owned_names.values():
                try:
                    cleaned = subprocess.run(["docker", "rm", "--force", name], capture_output=True, text=True, timeout=30)
                    records["owned_cleanup"].append({"name": name, "native_exit": cleaned.returncode,
                                                     "stdout": cleaned.stdout, "stderr": cleaned.stderr})
                    if cleaned.returncode and "No such container" not in cleaned.stderr:
                        cleanup_errors.append(name + ": " + cleaned.stderr)
                except (OSError, subprocess.TimeoutExpired) as error:
                    records["owned_cleanup"].append({"name": name, "error": str(error)})
                    cleanup_errors.append(name + ": " + str(error))
            if cleanup_errors:
                raise RuntimeError("Cannot remove owned HTTP fixtures: " + str(cleanup_errors))
            records["containers_after"] = command(["docker", "ps", "--all", "--quiet", "--no-trunc"]).splitlines()
            if records["containers_after"] != records["containers_before"]:
                raise ValueError("HTTP probe changed the container inventory")
        if command(["docker", "image", "inspect", tag, "--format={{.Id}}"]) != identifier:
            raise ValueError("HTTP probe fixture image changed")
        records["status"] = "PASS"
    finally:
        if records["status"] != "PASS":
            records["status"] = "FAIL"
        try:
            if tagged:
                arguments = ["docker", "tag", original, tag] if original else ["docker", "image", "rm", tag]
                result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
                records["tag_cleanup"] = {"native_exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
                if result.returncode:
                    records["status"] = "FAIL"
                    result.check_returncode()
        except (OSError, subprocess.SubprocessError):
            records["status"] = "FAIL"
            raise
        finally:
            report.write_json(evidence / "http-probe-cancellation.json", records)
