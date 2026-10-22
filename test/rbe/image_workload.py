"""Qualify sudo assembly and its four existing privilege-test consumers."""
import hashlib
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import qualification_report as report

from image_runtime import prepared_runtime

CASES = {"TestSudo", "TestSetUserId", "TestSetUserIdInFsWithNosuid", "TestFileCapWithNoNewPrivs"}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    targets = config["targets"]
    options = ["--compilation_mode=opt", "--lockfile_mode=error", "--noannounce_rc",
               "--remote_download_outputs=all", "--build_event_json_file_path_conversion=false"]
    failures = []
    for architecture in ("x86_64", "aarch64"):
        execution_log = evidence / (architecture + "-execution.json")
        built = invoke("sudo-" + architecture,
                       ["build", *options, "--config=" + architecture,
                        "--execution_log_json_file=" + str(execution_log),
                        "--noexecution_log_sort", *targets], "build", targets=targets)
        if built["status"] != "PASS":
            raise RuntimeError("Sudo image build failed for " + architecture)
        try:
            record_execution(execution_log, architecture)
        except (OSError, ValueError, RuntimeError) as error:
            # The reporter preserves this failure. Independent consumers may
            # continue only after the archive inspector proves their inputs.
            failures.append({"phase": "execution-metadata-" + architecture, "error": str(error)})
    contents_path = evidence / "sudo-contents.json"
    inspected = invoke("sudo-image-closure", ["run", "--lockfile_mode=error", "--noannounce_rc",
                        "@rules_python//python/bin:python", "--",
                        str(Path(__file__).with_name("sudo_image_inspect.py")), str(contents_path)],
                       "command", targets=[])
    if inspected["status"] != "PASS":
        failures.append({"phase": "archive-inspection", "status": inspected["status"]})
    contents = report.read_json(contents_path)
    inputs = [row.get("runtime_inputs") for row in contents["architectures"] if row["architecture"] == "amd64"]
    if len(inputs) != 1 or inputs[0] is None or inputs[0]["status"] != "PASS":
        raise RuntimeError("Sudo image lacks verified AMD64 runtime inputs")
    # Docker 24 denies unshare without SYS_ADMIN. This native test needs only
    # CLONE_NEWUSER; retain every other rule in its pinned default profile.
    profile_bytes = Path(__file__).with_name("docker-24.0.9-seccomp.json").read_bytes()
    if hashlib.sha256(profile_bytes).hexdigest() != "5e7994296d55cc3623fa4bd8e5682c36220c94e69b70ff2e923d3948d3a9995e":
        raise RuntimeError("Pinned Docker default seccomp profile changed")
    profile = json.loads(profile_bytes)
    profile["syscalls"].append({"names": ["unshare"], "action": "SCMP_ACT_ALLOW",
                                "args": [{"index": 0, "value": 0x10000000, "op": "SCMP_CMP_EQ"}]})
    profile_path = evidence / "sudo-userns-seccomp.json"
    profile_path.write_text(json.dumps(profile, indent=2) + "\n")
    configuration = {"runtime_name": "qualification-sudo", "image_name": "basic/sudo",
                     "runtime_flags": ["--platform=systrap", "--allow-suid"], "contents": str(contents_path),
                     "seccomp_profile": str(profile_path), "docker_version": "24.0.9",
                     "cleanup_reserve_seconds": config["cleanup_reserve_seconds"]}
    results = []
    destination = evidence / "sudo-consumers.json"
    try:
        with prepared_runtime(evidence / "runtime-preparation.json", private / "runtime", configuration):
            for runtime in ("runc", "qualification-sudo"):
                name = "sudo-consumer-" + runtime
                targets = ["//test/e2e:integration_test"]
                expression = "^(" + "|".join(sorted(CASES)) + ")$"
                test_options = [*options, "--config=x86_64", "--runs_per_test=1", "--flaky_test_attempts=1",
                                "--nocache_test_results", "--test_env=GO_TEST_WRAP_TESTV=1",
                                "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled",
                                "--strategy=TestRunner=local", "--local_test_jobs=1", "--test_timeout=900",
                                "--test_arg=-runtime=" + runtime, "--test_filter=" + expression,
                                "--test_arg=-test.run=" + expression]
                analysis = invoke(name + "-analysis", ["test", *test_options, "--nobuild", *targets],
                                  "analysis", targets=targets)
                if analysis["status"] != "PASS":
                    failures.append({"phase": name + "-analysis", "status": analysis["status"]})
                    continue
                tested = invoke(name, ["test", *test_options, *targets], "test",
                                (evidence / ("invocation-" + name + "-analysis") / "events.jsonl",), targets=targets)
                result = {"runtime": runtime, "status": "INCOMPLETE", "verdict": tested["status"]}
                results.append(result)
                try:
                    manifest = report.manifest_path(evidence / ("invocation-" + name) / "events.jsonl")
                    _, retained, errors = report.retained_reports(manifest)
                    xmls = [path for (_, member), path in retained.items() if member == "test.xml"]
                    if errors or len(xmls) != 1:
                        raise ValueError(f"Expected one authenticated consumer XML: {errors}; {xmls}")
                    cases = list(ET.parse(xmls[0]).getroot().iter("testcase"))
                    actual = [case.get("name") for case in cases]
                    result.update(xml=report.file_record(xmls[0]), cases=actual)
                    if len(actual) != len(CASES) or set(actual) != CASES:
                        raise ValueError(f"Required sudo cases differ: {actual}")
                    if tested["status"] != "PASS" or any(
                            case.find("skipped") is not None or case.find("failure") is not None or
                            case.find("error") is not None for case in cases):
                        raise RuntimeError("Required sudo consumer failed or skipped")
                    result["status"] = "PASS"
                except (OSError, ValueError, RuntimeError) as error:
                    result.update(status="FAIL", error=str(error))
                    failures.append({"phase": name, "error": str(error)})
    finally:
        report.write_json(destination, {"results": results, "failures": failures})
    if failures or len(results) != 2:
        raise RuntimeError(f"Sudo qualification has retained failures: {failures}")
