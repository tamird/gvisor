"""Run the existing Redis port-forward consumer with each supported network mode."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

import qualification_report as report
from image_runtime import materialize_retained_release, prepared_runtime


def run(invoke, evidence: Path, private: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    target = "//test/root:portforward_test"
    case = "TestPortForwardLocalMode"
    result = {"source": config["source"], "status": "INCOMPLETE", "modes": [], "commands": [],
              "runtime_source": config["runtime_supplier"]["source"],
              "scope": "AMD64 actual port forwarding; ARM64 image inspection only"}

    def command(arguments: list[str], timeout: float) -> str:
        row = {"arguments": arguments, "native_exit": None}
        result["commands"].append(row)
        proc = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
        row.update(native_exit=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
        proc.check_returncode()
        return proc.stdout

    before = sorted(command(["docker", "ps", "-aq", "--no-trunc"], 30).split())
    try:
        runtime_directory = private / "redis-runtime"
        retained = materialize_retained_release(config["runtime_supplier"], runtime_directory, evidence, "redis")
        for mode, network in (("netstack", "sandbox"), ("hostnet", "host")):
            runtime = "qualification-redis-" + mode + "-" + uuid.uuid4().hex
            directory = evidence / ("redis-" + mode)
            directory.mkdir()
            row = {"mode": mode, "status": "INCOMPLETE"}
            result["modes"].append(row)
            setup = {"runtime_name": runtime, "image_name": "basic/redis",
                     "runtime_flags": ["--platform=systrap", "--network=" + network],
                     "contents": str(evidence / "redis-contents.json"),
                     "retained_release_files": retained, "release_supplier": config["runtime_supplier"]}
            with prepared_runtime(directory / "runtime-preparation.json", runtime_directory, setup) as prepared:
                files = {str(Path(item["path"]).relative_to(runtime_directory)): item for item in prepared["runtime_files"]}
                if set(files) != set(retained) or any(any(files[name][key] != item[key] for key in ("bytes", "sha256")) for name, item in retained.items()):
                    raise ValueError("Installed runtime differs from the retained release")
                options = [*config["consumer_options"], "--compilation_mode=opt", "--config=x86_64",
                           "--lockfile_mode=error", "--noannounce_rc", "--remote_download_outputs=all",
                           "--build_event_json_file_path_conversion=false", "--nozip_undeclared_test_outputs",
                           "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                           "--test_sharding_strategy=disabled", "--strategy=TestRunner=local", "--local_test_jobs=1",
                           "--test_timeout=300", "--test_env=GO_TEST_WRAP_TESTV=1",
                           "--test_arg=-runtime=" + runtime, "--test_arg=-test.v=test2json",
                           "--test_filter=^" + case + "$", "--test_arg=-test.run=^" + case + "$"]
                phase = "redis-" + mode + "-consumer"
                analyzed = invoke(phase + "-analysis", ["test", *options, "--nobuild", target], "analysis", targets=[target])
                if analyzed["status"] != "PASS":
                    raise RuntimeError("Port-forward owner analysis failed: " + mode)
                tested = invoke(phase, ["test", *options, target], "test",
                                (evidence / ("invocation-" + phase + "-analysis/events.jsonl"),), targets=[target])
                _, reports, errors = report.retained_reports(report.manifest_path(evidence / ("invocation-" + phase + "/events.jsonl")))
                if errors:
                    raise ValueError("Cannot authenticate port-forward reports: " + str(errors))
                xmls = [(attempt, path) for (attempt, name), path in reports.items() if name == "test.xml"]
                match xmls:
                    case [(attempt, xml)] if attempt.owner.label == target:
                        pass
                    case _:
                        raise ValueError("Expected one actual port-forward XML owner")
                names = [entry.get("name") for entry in ET.parse(xml).getroot().iter("testcase")]
                counts = report.xml_cases(xml)
                row.update(attempt=asdict(attempt), xml=report.file_record(xml), names=names,
                           counts=counts, native_attempts=tested["executed"])
                if names != [case] or counts["passed"] != 1 or counts["failed"] or counts["skipped"]:
                    raise ValueError("Port-forward case failed, skipped or differs from selection")
                if tested["status"] != "PASS" or len(tested["executed"]) != 1 or any(
                        item["cached_locally"] or item["execution_info"].get("cachedRemotely", False) for item in tested["executed"]):
                    raise ValueError("Port-forward case did not execute once without a cached result")
                if mode == "netstack":
                    row["versions"] = {}
                    for program in ("redis-server", "redis-cli"):
                        name = runtime + "-" + program
                        try:
                            remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError("Redis version observation exhausted the work deadline")
                            version = command(["docker", "run", "--rm", "--name=" + name, "--runtime=" + runtime,
                                               prepared["image"]["id"], program, "--version"], min(30, remaining)).strip()
                            if program == "redis-server":
                                valid = re.search(r"^Redis server v=7\.0\.12(?: |$)", version) is not None
                            else:
                                valid = version == "redis-cli 7.0.12"
                            if not valid:
                                raise ValueError("Actual Redis version differs: " + version)
                            row["versions"][program] = version
                        finally:
                            cleanup = subprocess.run(["docker", "rm", "--force", name], capture_output=True, text=True, timeout=30)
                            result["commands"].append({"arguments": cleanup.args, "native_exit": cleanup.returncode,
                                                       "stdout": cleanup.stdout, "stderr": cleanup.stderr,
                                                       "absent_allowed": True})
                            if cleanup.returncode and "No such container" not in cleanup.stderr:
                                raise RuntimeError("Cannot remove owned Redis version container")
            row["status"] = "PASS"
        result["status"] = "PASS"
    finally:
        completed = result["status"]
        result["status"] = "FAIL"
        try:
            after = sorted(command(["docker", "ps", "-aq", "--no-trunc"], 30).split())
            result["container_inventory"] = {"before": before, "after": after}
            if after != before:
                raise RuntimeError("Redis qualification changed container inventory")
            result["cleanup"] = "PASS"
            result["status"] = completed
        finally:
            report.write_json(evidence / "redis-runtime-results.json", result)
