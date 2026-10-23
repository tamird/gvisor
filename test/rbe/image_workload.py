"""Check the changed readiness fixture with canonical Go formatting and Nogo."""
import json
import os
from pathlib import Path

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    owner = "//test/root:portforward_test_nogo"
    if config["targets"] != [owner]:
        raise ValueError("Unexpected readiness analyzer owner")
    record = {"source": config["source"], "readiness": config["readiness"],
              "status": "INCOMPLETE", "architectures": []}
    try:
        formatted = invoke("gofmt", ["run", "--lockfile_mode=error", "--noannounce_rc",
                           "@rules_python//python/bin:python", "--", "-c",
                           'import os, subprocess, sys; os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"]); sys.exit(subprocess.run(["bash", "tools/lint.sh", "gofmt"]).returncode)'],
                           "command", targets=[])
        record["gofmt"] = formatted["status"]
        if formatted["status"] != "PASS":
            raise RuntimeError("Canonical Go formatting failed")
        for architecture in ("x86_64", "aarch64"):
            result = invoke("nogo-" + architecture, ["test", "--compilation_mode=opt",
                            "--config=" + architecture, "--lockfile_mode=error",
                            "--remote_download_outputs=all", "--noannounce_rc",
                            "--build_event_json_file_path_conversion=false", "--keep_going",
                            "--nocache_test_results", "--test_output=all", owner], "test", targets=[owner])
            record["architectures"].append({"architecture": architecture,
                "status": result["status"], "case_counts": result["case_counts"]})
            if result["status"] != "PASS" or result["case_counts"] != {"failed": 0, "passed": 1, "skipped": 0}:
                raise RuntimeError("Readiness analyzer failed for " + architecture)
        record["status"] = "PASS"
    finally:
        report.write_json(evidence / "portforward-checks.json", record)
