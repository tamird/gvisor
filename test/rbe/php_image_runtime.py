"""Complete the existing PHP consumer without replaying raw fixture comparisons."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

import qualification_report as report

from image_archive import identity
from image_runtime import prepared_runtime

SAMPLES = [
    "tests/basic/001.phpt",
    "sapi/cli/tests/001.phpt",
    "sapi/cgi/tests/001.phpt",
    "sapi/phpdbg/tests/basic_run.phpt",
    "ext/sodium/tests/crypto_hex.phpt",
    "ext/bcmath/tests/GHSA-x692-q9x7-8c3f.phpt",
    "ext/pdo_pgsql/tests/transations_deprecations.phpt",
]


def run(invoke, evidence: Path, private: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    contents = json.loads((evidence / "php-contents.json").read_text())
    proctor = contents["proctor"]["producer"]["output"]
    if contents["status"] != "PASS" or contents["source"] != config["source"] or identity(Path(proctor["path"])) != proctor:
        raise ValueError("PHP runtime requires exact inspected fixture and proctor bytes")
    prior = config["prior_observations"]
    if {key: proctor[key] for key in ("sha256", "bytes")} != prior["proctor"]:
        raise ValueError("PHP consumer proctor differs from retained raw observations")
    for row in contents["architectures"]:
        expected = prior["images"][row["architecture"]]
        if row["config_sha256"] != expected["config_sha256"] or any(
                {key: row[kind][key] for key in ("sha256", "bytes")} != expected[kind]
                for kind in ("archive", "context")):
            raise ValueError("PHP image differs from retained raw observations")
    result = {"source": config["source"], "status": "INCOMPLETE", "samples": SAMPLES,
              "proctor": contents["proctor"], "prior_observations": prior}
    runtime = "qualification-php-" + uuid.uuid4().hex
    setup = {"runtime_name": runtime, "image_name": "runtimes/php8.5.11",
             "runtime_flags": ["--platform=systrap", "--network=sandbox"],
             "contents": str(evidence / "php-contents.json")}
    before = sorted(subprocess.check_output(["docker", "ps", "-aq", "--no-trunc"], text=True, timeout=30).split())
    try:
        with prepared_runtime(evidence / "php-runtime-preparation.json", private / "php-runtime", setup):
            target = "//test/runtimes:php8.5.11"
            options = ["--compilation_mode=opt", "--config=x86_64", "--lockfile_mode=error",
                       "--noannounce_rc", "--remote_download_outputs=all",
                       "--build_event_json_file_path_conversion=false", "--nozip_undeclared_test_outputs",
                       "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                       "--test_sharding_strategy=disabled", "--strategy=TestRunner=local", "--local_test_jobs=1",
                       "--test_timeout=600", "--test_arg=--runtime=" + runtime,
                       "--test_arg=--batch=1", "--test_arg=--tests=" + ",".join(SAMPLES)]
            analyzed = invoke("php-consumer-analysis", ["test", *options, "--nobuild", target], "analysis", targets=[target])
            if analyzed["status"] != "PASS":
                raise RuntimeError("Existing PHP runtime owner analysis failed")
            tested = invoke("php-consumer", ["test", *options, target], "test",
                            (evidence / "invocation-php-consumer-analysis/events.jsonl",), targets=[target])
            _, retained, errors = report.retained_reports(report.manifest_path(evidence / "invocation-php-consumer/events.jsonl"))
            if errors:
                raise ValueError("Cannot authenticate PHP consumer reports: " + str(errors))
            xmls = [(attempt, path) for (attempt, name), path in retained.items() if name == "test.xml"]
            logs = [(attempt, path) for (attempt, name), path in retained.items() if name == "test.log"]
            match xmls, logs:
                case [(attempt, xml)], [(log_attempt, log)] if attempt == log_attempt and attempt.owner.label == target:
                    pass
                case _:
                    raise ValueError("Expected one matching actual PHP runtime XML/log attempt")
            raw = log.read_text()
            markers = re.findall(r"Running test case batch: ([^\n]+)|^(PASS): \([^\n]+\) 1 tests passed$", raw, re.M)
            selected = []
            for offset in range(0, len(markers), 2):
                match markers[offset:offset + 2]:
                    case [(name, ""), ("", "PASS")] if name in SAMPLES:
                        selected.append(name)
                    case other:
                        raise ValueError(f"Unmatched PHP batch/result sequence: {other}")
            if sorted(selected) != sorted(SAMPLES):
                raise ValueError("Existing PHP owner did not complete each of the seven selected batches")
            if tested["status"] != "PASS" or len(tested["executed"]) != 1 or any(
                    item["cached_locally"] or item["execution_info"].get("cachedRemotely", False) for item in tested["executed"]):
                raise ValueError("Existing PHP runtime consumer did not pass in one uncached attempt")
            counts = report.xml_cases(xml)
            if not counts["passed"] or counts["failed"] or counts["skipped"]:
                raise ValueError("Existing PHP runtime XML is empty, failed or skipped")
            result["consumer"] = {"owner": target, "attempt": asdict(attempt), "xml": report.file_record(xml),
                                  "log": report.file_record(log), "counts": counts, "selected": selected,
                                  "passed_batches": selected, "native_attempts": tested["executed"],
                                  "xml_scope": "Bazel runtime rule wrapper; exact seven batches independently checked in authenticated native log"}
        result["status"] = "PASS"
    finally:
        completed = result["status"]
        result["status"] = "FAIL"
        try:
            after = sorted(subprocess.check_output(["docker", "ps", "-aq", "--no-trunc"], text=True, timeout=30).split())
            result["container_inventory"] = {"before": before, "after": after}
            if after != before:
                raise RuntimeError("PHP consumer changed container inventory")
            result["cleanup"] = "PASS"
            result["status"] = completed
        finally:
            report.write_json(evidence / "php-runtime-results.json", result)
