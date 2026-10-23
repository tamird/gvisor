"""Run the existing Nginx consumers against the inspected upstream-based image."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

import qualification_report as report
from image_runtime import prepared_runtime


def run(invoke, evidence: Path, private: Path) -> None:
    owners = {
        "//test/e2e:integration_test": "TestLifeCycle",
        "//test/image:image_test": "TestNginx",
        "//test/root:portforward_test": "TestPortForwardStreamMode",
    }
    contents = report.read_json(evidence / "nginx-contents.json")
    amd64 = next(row for row in contents["architectures"] if row["architecture"] == "amd64")
    records = {"status": "INCOMPLETE", "modes": [], "scope": "AMD64 runtime; ARM64 image inspection only"}
    try:
        for mode, network in (("netstack", "sandbox"), ("hostnet", "host")):
            selected = owners if mode == "netstack" else {"//test/root:portforward_test": owners["//test/root:portforward_test"]}
            runtime = "qualification-nginx-" + mode
            directory = evidence / ("nginx-" + mode)
            directory.mkdir()
            row = {"mode": mode, "status": "INCOMPLETE", "expected": selected, "executed": []}
            records["modes"].append(row)
            configuration = {"runtime_name": runtime, "image_name": "basic/nginx",
                             "runtime_flags": ["--platform=systrap", "--network=" + network],
                             "contents": str(evidence / "nginx-contents.json")}
            with prepared_runtime(directory / "runtime-preparation.json", private / runtime, configuration):
                if mode == "netstack":
                    # Use the just-loaded image, with its verified immutable
                    # configuration ID, for the executable's own diagnostics.
                    container = runtime + "-version-" + uuid.uuid4().hex
                    version = {"status": "INCOMPLETE", "expected_version": "1.31.7", "provenance": "pinned upstream image"}
                    row["version"] = version
                    arguments = ["docker", "run", "--rm", "--name=" + container,
                                 "--runtime=" + runtime, "--entrypoint=/usr/sbin/nginx",
                                 "sha256:" + amd64["config_sha256"], "-V"]
                    version["arguments"] = arguments
                    try:
                        remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Nginx version check exhausted the existing work deadline")
                        result = subprocess.run(arguments, capture_output=True, text=True, timeout=remaining)
                        version["native_exit"] = result.returncode
                        output = result.stdout + result.stderr
                        (directory / "nginx-version.log").write_text(output)
                        version["output"] = report.file_record(directory / "nginx-version.log")
                        result.check_returncode()
                        configured = [line.removeprefix("configure arguments: ").strip() for line in output.splitlines()
                                      if line.startswith("configure arguments: ")]
                        if "nginx version: nginx/1.31.7" not in output.splitlines() or len(configured) != 1:
                            raise ValueError("Runtime Nginx version or configure diagnostics differ")
                        version["configure_arguments"] = configured[0]
                        version["status"] = "PASS"
                    finally:
                        # --rm normally removes it. A timeout may leave it live;
                        # remove only this task-owned container in the reserve.
                        cleanup = subprocess.run(["docker", "container", "rm", "--force", container],
                                                 capture_output=True, text=True, timeout=30)
                        version["cleanup_command"] = {"arguments": cleanup.args, "native_exit": cleanup.returncode,
                                                      "stdout": cleanup.stdout, "stderr": cleanup.stderr}
                        if cleanup.returncode and "No such container" not in cleanup.stderr:
                            version["cleanup"] = "FAIL"
                            raise RuntimeError("Cannot clean up the Nginx diagnostic container: " + cleanup.stderr)
                        version["cleanup"] = "PASS"
                pattern = "^(" + "|".join(selected.values()) + ")$"
                targets = list(selected)
                options = ["--compilation_mode=opt", "--lockfile_mode=error", "--noannounce_rc",
                           "--remote_download_outputs=all", "--build_event_json_file_path_conversion=false",
                           "--config=x86_64", "--runs_per_test=1", "--flaky_test_attempts=1",
                           "--nocache_test_results", "--test_env=GO_TEST_WRAP_TESTV=1",
                           "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled",
                           "--strategy=TestRunner=local", "--local_test_jobs=1", "--test_timeout=900",
                           "--test_arg=-runtime=" + runtime, "--test_arg=-test.v=test2json",
                           "--test_filter=" + pattern, "--test_arg=-test.run=" + pattern]
                phase = "nginx-" + mode + "-consumers"
                analyzed = invoke(phase + "-analysis", ["test", *options, "--nobuild", *targets],
                                  "analysis", targets=targets)
                if analyzed["status"] != "PASS":
                    raise RuntimeError("Existing Nginx consumer analysis failed: " + mode)
                tested = invoke(phase, ["test", *options, *targets], "test",
                                (evidence / ("invocation-" + phase + "-analysis") / "events.jsonl",), targets=targets)
                manifest = report.manifest_path(evidence / ("invocation-" + phase) / "events.jsonl")
                _, retained, errors = report.retained_reports(manifest)
                if errors:
                    raise ValueError("Cannot authenticate Nginx consumer reports: " + str(errors))
                xmls = [(attempt, path) for (attempt, name), path in retained.items() if name == "test.xml"]
                if len(xmls) != len(selected) or {attempt.owner.label for attempt, _ in xmls} != set(selected):
                    raise ValueError("Nginx XML owner set differs from the selected consumers")
                for attempt, path in xmls:
                    names = [case.get("name") for case in ET.parse(path).getroot().iter("testcase")]
                    counts = report.xml_cases(path)
                    row["executed"].append({"attempt": asdict(attempt), "xml": report.file_record(path),
                                             "names": names, "counts": counts})
                    if names != [selected[attempt.owner.label]] or counts["passed"] != 1 or counts["failed"] or counts["skipped"]:
                        raise ValueError("Nginx consumer omitted, added, failed or skipped a selected case")
                row["native_attempts"] = tested["executed"]
                if len(tested["executed"]) != len(selected) or any(
                        attempt["cached_locally"] or attempt["execution_info"].get("cachedRemotely", False)
                        for attempt in tested["executed"]):
                    raise ValueError("Nginx consumers did not execute as distinct uncached attempts")
                if tested["status"] != "PASS" or tested["case_counts"].get("passed") != len(selected) or tested["case_counts"].get("skipped", 0):
                    raise RuntimeError("Existing Nginx consumers did not all pass: " + mode)
            row["status"] = "PASS"
        records["status"] = "PASS"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        records.update(status="FAIL", error=str(error))
        raise
    finally:
        report.write_json(evidence / "nginx-runtime-results.json", records)
