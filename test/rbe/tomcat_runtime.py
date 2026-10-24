"""Exercise the existing native and systrap Tomcat HTTP consumers."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import uuid
import xml.etree.ElementTree as ET

import qualification_report as report
from image_runtime import materialize_retained_release, prepared_runtime


def run(invoke, evidence: Path, private: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    records = {"source": config["source"], "status": "INCOMPLETE", "commands": [], "modes": [],
               "runtime_source": config["runtime_supplier"]["source"]}

    def inventory() -> list[str]:
        arguments = ["docker", "ps", "-aq", "--no-trunc"]
        row = {"arguments": arguments, "native_exit": None}
        records["commands"].append(row)
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
        row.update(native_exit=result.returncode, stdout=result.stdout, stderr=result.stderr)
        result.check_returncode()
        return sorted(result.stdout.split())

    before = None
    try:
        before = inventory()
        runtime_directory = private / "tomcat-runtime"
        retained = materialize_retained_release(config["runtime_supplier"], runtime_directory, evidence, "tomcat")
        runtime = "qualification-tomcat-" + uuid.uuid4().hex
        records["runtime_name"] = runtime
        setup = {"runtime_name": runtime, "image_name": "basic/tomcat",
                 "runtime_flags": ["--platform=systrap", "--network=sandbox"],
                 "contents": str(evidence / "tomcat-contents.json"),
                 "retained_release_files": retained, "release_supplier": config["runtime_supplier"]}
        with prepared_runtime(evidence / "tomcat-runtime-preparation.json", runtime_directory, setup) as prepared:
            files = {str(Path(item["path"]).relative_to(runtime_directory)): item for item in prepared["runtime_files"]}
            if set(files) != set(retained) or any(
                    any(files[name][key] != item[key] for key in ("bytes", "sha256")) for name, item in retained.items()):
                raise ValueError("Installed runtime differs from the retained release")
            expected = json.loads((Path(__file__).with_name("tomcat_upstream") / "amd64-config.json").read_text())
            loaded = prepared["built_image"]
            for key, value in expected["config"].items():
                if (loaded["Config"].get(key) or None) != (value or None):
                    raise ValueError(f"Loaded Tomcat config differs: {key}")
            records["loaded_config"] = loaded["Config"]
            for mode, selected_runtime in (("native", "runc"), ("runsc", runtime)):
                target = "//test/image:image_test"
                phase = "tomcat-" + mode
                row = {"mode": mode, "target": target, "runtime": selected_runtime, "status": "INCOMPLETE"}
                records["modes"].append(row)
                options = [*config["consumer_options"], "--compilation_mode=opt", "--config=x86_64",
                           "--lockfile_mode=error", "--noannounce_rc", "--remote_download_outputs=all",
                           "--build_event_json_file_path_conversion=false", "--nozip_undeclared_test_outputs",
                           "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                           "--test_sharding_strategy=disabled", "--strategy=TestRunner=local", "--local_test_jobs=1",
                           "--test_timeout=300", "--test_env=GO_TEST_WRAP_TESTV=1",
                           "--test_arg=-runtime=" + selected_runtime, "--test_arg=-test.v=test2json",
                           "--test_filter=^TestTomcat$", "--test_arg=-test.run=^TestTomcat$"]
                analyzed = invoke(phase + "-analysis", ["test", *options, "--nobuild", target], "analysis", targets=[target])
                if analyzed["status"] != "PASS":
                    raise RuntimeError("Tomcat consumer owner analysis failed: " + mode)
                tested = invoke(phase, ["test", *options, target], "test",
                                (evidence / ("invocation-" + phase + "-analysis/events.jsonl"),), targets=[target])
                row["native_attempts"] = tested["executed"]
                manifest = report.manifest_path(evidence / ("invocation-" + phase) / "events.jsonl")
                _, reports, errors = report.retained_reports(manifest)
                if errors:
                    raise ValueError("Cannot authenticate Tomcat reports: " + str(errors))
                match [(attempt, path) for (attempt, name), path in reports.items() if name == "test.xml"]:
                    case [(attempt, xml)] if attempt.owner.label == target:
                        pass
                    case _:
                        raise ValueError("Expected one actual Tomcat XML owner")
                names = [entry.get("name") for entry in ET.parse(xml).getroot().iter("testcase")]
                counts = report.xml_cases(xml)
                row.update(attempt=asdict(attempt), xml=report.file_record(xml), names=names, counts=counts)
                if names != ["TestTomcat"] or counts["passed"] != 1 or counts["failed"] or counts["skipped"]:
                    raise ValueError("Tomcat case failed, skipped or differs from selection")
                if tested["status"] != "PASS" or len(tested["executed"]) != 1 or any(
                        item["cached_locally"] or item["execution_info"].get("cachedRemotely", False) for item in tested["executed"]):
                    raise ValueError("Tomcat case did not execute once without a cached result")
                if inventory() != before:
                    raise RuntimeError("Tomcat consumer left containers after completion")
                row["status"] = "PASS"
        records["status"] = "PASS"
    finally:
        completed = records["status"]
        records["status"] = "FAIL"
        try:
            after = inventory()
            records["container_inventory"] = {"before": before, "after": after}
            if before is None or after != before:
                raise RuntimeError("Tomcat qualification changed container inventory")
            records["cleanup"] = "PASS"
            records["status"] = completed
        finally:
            report.write_json(evidence / "tomcat-runtime-results.json", records)
