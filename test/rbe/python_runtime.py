"""Exercise the existing Python HTTP pause and checkpoint consumers."""
from dataclasses import asdict
import json
import os
import re
from pathlib import Path
import subprocess
import uuid
import xml.etree.ElementTree as ET

import qualification_report as report
from image_runtime import materialize_retained_release, prepared_runtime


def run(invoke, evidence: Path, private: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    target = "//test/e2e:integration_test"
    cases = {"TestPauseResume", "TestCheckpointRestore"}
    case_filter = "^(TestPauseResume|TestCheckpointRestore)$"
    phase = "python-consumer"
    records = {"source": config["source"], "status": "INCOMPLETE", "commands": [],
               "runtime_source": config["runtime_supplier"]["source"]}

    def command(arguments: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        row = {"arguments": arguments, "native_exit": None}
        records["commands"].append(row)
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
        row.update(native_exit=result.returncode, stdout=result.stdout, stderr=result.stderr)
        result.check_returncode()
        return result

    def inventory() -> list[str]:
        return sorted(command(["docker", "ps", "-aq", "--no-trunc"]).stdout.split())

    before = None
    try:
        before = inventory()
        runtime_directory = private / "python-runtime"
        retained = materialize_retained_release(config["runtime_supplier"], runtime_directory, evidence, "python")
        runtime = "qualification-python-" + uuid.uuid4().hex
        records["runtime_name"] = runtime
        setup = {"runtime_name": runtime, "image_name": "basic/python",
                 "runtime_flags": ["--platform=systrap", "--network=sandbox"],
                 "contents": str(evidence / "python-contents.json"),
                 "retained_release_files": retained, "release_supplier": config["runtime_supplier"],
                 "docker_experimental": True, "docker_version": "24.0.9"}
        with prepared_runtime(evidence / "python-runtime-preparation.json", runtime_directory, setup) as prepared:
            files = {str(Path(item["path"]).relative_to(runtime_directory)): item for item in prepared["runtime_files"]}
            if set(files) != set(retained) or any(
                    any(files[name][key] != item[key] for key in ("bytes", "sha256")) for name, item in retained.items()):
                raise ValueError("Installed runtime differs from the retained release")
            info = prepared["server_info"]
            if info["ServerVersion"] != "24.0.9" or not info["ExperimentalBuild"]:
                raise ValueError("Python checkpoint requires Docker 24.0.9 experimental mode")
            loaded = prepared["built_image"]
            expected = json.loads((Path(__file__).with_name("python_upstream") / "amd64-config.json").read_text())["config"]
            if loaded["Config"]["Entrypoint"] != ["python", "-m", "http.server", "8080"] or loaded["Config"].get("Cmd"):
                raise ValueError("Loaded Python HTTP command differs")
            if loaded["Config"]["Env"] != expected["Env"]:
                raise ValueError("Loaded Python environment differs from the upstream config")
            created = command(["docker", "create", "--name", runtime + "-version", "--runtime", runtime,
                               "--entrypoint", "python", loaded["Id"], "--version"])
            version_id = created.stdout.strip()
            if re.fullmatch(r"[0-9a-f]{64}", version_id) is None:
                raise ValueError("Docker did not return the owned Python version container ID")
            records["version_container_id"] = version_id
            try:
                version = command(["docker", "start", "--attach", version_id], timeout=60)
                if version.stdout.strip() != "Python 3.15.1" or version.stderr.strip():
                    raise ValueError("Actual Python version differs from the selected upstream image")
                state = json.loads(command(["docker", "inspect", "--format={{json .State}}", version_id]).stdout)
                records["version_container_state"] = state
                if state["Status"] != "exited" or state["Running"] or state["ExitCode"] != 0 or state["Error"]:
                    raise ValueError("Python version container did not exit successfully")
                records["version"] = version.stdout.strip()
            finally:
                command(["docker", "rm", "--force", version_id])
            records["loaded_config"] = loaded["Config"]
            if inventory() != before:
                raise RuntimeError("Python version command left containers")
            options = [*config["consumer_options"], "--compilation_mode=opt", "--config=x86_64",
                       "--lockfile_mode=error", "--noannounce_rc", "--remote_download_outputs=all",
                       "--build_event_json_file_path_conversion=false", "--nozip_undeclared_test_outputs",
                       "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                       "--test_sharding_strategy=disabled", "--strategy=TestRunner=local", "--local_test_jobs=1",
                       "--test_timeout=300", "--test_env=GO_TEST_WRAP_TESTV=1",
                       "--test_arg=-runtime=" + runtime, "--test_arg=-test.v=test2json",
                       "--test_arg=-test-checkpoint=true",
                       "--test_filter=" + case_filter, "--test_arg=-test.run=" + case_filter]
            analyzed = invoke(phase + "-analysis", ["test", *options, "--nobuild", target], "analysis", targets=[target])
            if analyzed["status"] != "PASS":
                raise RuntimeError("Python consumer owner analysis failed")
            tested = invoke(phase, ["test", *options, target], "test",
                            (evidence / ("invocation-" + phase + "-analysis/events.jsonl"),), targets=[target])
            records["native_attempts"] = tested["executed"]
            manifest = report.manifest_path(evidence / ("invocation-" + phase) / "events.jsonl")
            _, reports, errors = report.retained_reports(manifest)
            if errors:
                raise ValueError("Cannot authenticate Python reports: " + str(errors))
            match [(attempt, path) for (attempt, name), path in reports.items() if name == "test.xml"]:
                case [(attempt, xml)] if attempt.owner.label == target:
                    pass
                case _:
                    raise ValueError("Expected one actual Python XML owner")
            names = [entry.get("name") for entry in ET.parse(xml).getroot().iter("testcase")]
            counts = report.xml_cases(xml)
            records.update(attempt=asdict(attempt), xml=report.file_record(xml), names=names, counts=counts)
            if len(names) != 2 or set(names) != cases or counts["passed"] != 2 or counts["failed"] or counts["skipped"]:
                raise ValueError("Python case failed, skipped or differs from selection")
            if tested["status"] != "PASS" or len(tested["executed"]) != 1 or any(
                    item["cached_locally"] or item["execution_info"].get("cachedRemotely", False) for item in tested["executed"]):
                raise ValueError("Python case did not execute once without a cached result")
            if inventory() != before:
                raise RuntimeError("Python consumer left containers after completion")
        records["status"] = "PASS"
    finally:
        completed = records["status"]
        records["status"] = "FAIL"
        try:
            after = inventory()
            records["container_inventory"] = {"before": before, "after": after}
            if before is None or after != before:
                raise RuntimeError("Python qualification changed container inventory")
            records["cleanup"] = "PASS"
            records["status"] = completed
        finally:
            report.write_json(evidence / "python-runtime-results.json", records)
