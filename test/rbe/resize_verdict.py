"""Require both unchanged restore subtests with a fresh local TestRunner result."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import qualification_report as report


def main():
    data = Path("/evidence")
    owner = "//runsc/container:container_test"
    cases = [f"container/TestRestoreCgroupMemoryLimit/{name}" for name in ("createAtLimit", "lowerBeforeRestore")]
    summary = {"expected": cases, "status": "INCOMPLETE"}
    try:
        events = data / "events.jsonl"
        capture = report.capture(data / "raw.jsonl", events, Path("/src/bazel-out"), data / "reports", expected_kind="test")
        (data / "capture_exit").write_text("1" if capture["errors"] else "0")
        keys = "executed_arguments commit_file source_before source_after native_exit capture_exit final_head".split()
        report.write_json(data / "context.json", {**{key: key for key in keys}, "expected_source": os.environ["PRODUCT"],
                          "command_kind": "test", "execution_finished_at": (data / "finished").read_text().strip()})
        (data / "owners").write_text(owner + "\n")
        report.write_json(data / "required.json", {owner: cases})
        verdict = report.verdict(events, report.manifest_path(events), data / "context.json", data / "owners",
                                 case_manifest=data / "required.json")
        summary.update(qualified_owner_status=verdict["status"], counts=verdict["case_counts"],
                       unpassed_required_cases=[r["name"] for r in verdict["unpassed_required_cases"] if r["name"] in cases], missing_owners=len(verdict["missing"]))
        attempts = verdict["executed"]
        fresh = (len(attempts) == 1 and attempts[0]["owner"]["label"] == owner and not attempts[0]["cached_locally"]
                 and not attempts[0]["execution_info"].get("cachedRemotely")
                 and attempts[0]["execution_info"].get("strategy") == "local")
        summary["fresh_local_result"] = fresh
        summary["taken_at"] = verdict["verdict_available_at"]
        if verdict["status"] == "PASS" and fresh and not verdict["skips"]:
            summary["status"] = "PASS"
        elif verdict["status"] == "FAIL":
            summary["status"] = "FAIL"
        summary["native_files"] = {
            ext: report.file_record(next((data / "reports").rglob("test." + ext)))
            for ext in ("xml", "log")
        }
    except (OSError, KeyError, ValueError, StopIteration) as error:
        summary.update(status="INCOMPLETE", reporter_error=type(error).__name__)
    summary["verified_utc"] = datetime.now(timezone.utc).isoformat()
    Path("/verified/summary.json").write_text(json.dumps(summary) + "\n")
    return summary["status"] == "PASS"


if __name__ == "__main__":
    sys.exit(not main())
