"""Run the existing reporter's retained-source controls without workload replay."""
import hashlib
import json
import os
from pathlib import Path
import re

import qualification_report as report


OWNER = "//test/rbe:qualification_report_test"
TESTS = [
    "test_explicit_source_set_preserves_retained_failure",
    "test_explicit_source_set_receipt_requires_exact_sources",
    "test_explicit_source_set_rejects_invalid_contracts",
    "test_receipt_preserves_retained_outcomes",
    "test_receipt_rejects_wrong_artifact_source_and_targets",
    "test_empty_bundle_never_passes",
    "test_bundle_requires_every_invocation_and_excludes_aggregate",
]


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    for name, expected in config["matched_inputs"].items():
        path = Path(name)
        raw = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Source input differs: " + name)
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    result = invoke("reporter-controls", ["test", *options, "--strategy=TestRunner=local",
                    "--nocache_test_results", "--runs_per_test=1", "--flaky_test_attempts=1",
                    "--test_sharding_strategy=disabled", "--test_output=all", "--test_timeout=180",
                    *["--test_arg=QualificationReportTest." + name for name in TESTS], OWNER],
                    "test", targets=[OWNER])
    if result["status"] != "PASS":
        raise RuntimeError("Existing reporter controls did not pass")
    match result["executed"]:
        case [attempt] if (attempt["owner"]["label"] == OWNER and attempt["status"] == "PASSED"
                           and not attempt["cached_locally"]
                           and (attempt["run"], attempt["attempt"], attempt["shard"]) == (1, 1, 1)):
            pass
        case _:
            raise ValueError("Unexpected native reporter test attempt")
    directory = evidence / "invocation-reporter-controls"
    _, reports, errors = report.retained_reports(report.manifest_path(directory / "events.jsonl"))
    if errors:
        raise ValueError(str(errors))
    logs = [path for (identity, name), path in reports.items() if identity.owner.label == OWNER and name == "test.log"]
    match logs:
        case [log] if re.search(r"Ran 7 tests in [0-9.]+s\s+OK\s*$", log.read_text()):
            pass
        case _:
            raise ValueError("Missing exact seven-control native completion")
    report.write_json(evidence / "expected-source-controls.json", {"status": "PASS", "source": config["source"],
                      "owner": OWNER, "tests": TESTS, "log": report.file_record(log),
                      "retained_original_status": "FAIL", "runtime_replayed": False})
