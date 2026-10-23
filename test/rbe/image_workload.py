"""Complete the one interrupted NVIDIA parser analyzer owner."""

import json
import os
from pathlib import Path

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    match config["test_groups"]:
        case [group]:
            pass
        case _:
            raise ValueError("Expected exactly one remaining analyzer group")
    if group["targets"] != ["//pkg/sentry/devices/nvproxy:nvproxy_driver_parity_test_nogo"]:
        raise ValueError("Unexpected analyzer completion owner")
    record = {"source": config["source"], "product": config["product"],
              "status": "INCOMPLETE", "targets": group["targets"]}
    try:
        result = invoke(group["name"], ["test", "--compilation_mode=opt",
                        "--lockfile_mode=error", "--remote_download_outputs=all",
                        "--noannounce_rc", "--build_event_json_file_path_conversion=false",
                        *group["options"], "--keep_going", "--nocache_test_results",
                        "--test_output=all", *group["targets"]], "test", targets=group["targets"])
        record["case_counts"] = result["case_counts"]
        record["native_status"] = result["status"]
        if result["status"] != "PASS" or result["case_counts"] != group["expected_cases"]:
            raise RuntimeError("Remaining NVIDIA parser analyzer did not pass")
        record["status"] = "PASS"
    finally:
        report.write_json(evidence / "compiler-parser-nogo.json", record)
