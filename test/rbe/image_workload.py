"""Check command verdicts and reconcile one retained successful action query."""
import hashlib
import json
import os
from pathlib import Path
import tarfile
import time
import urllib.request

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    owner = "//test/rbe:qualification_report_test"
    if config["targets"] != [owner]:
        raise ValueError("Unexpected reporter regression owner")
    result = {"status": "INCOMPLETE", "source": config["source"], "supplier": config["retained_query"]}
    try:
        tested = invoke("command-regression", [
            "test", "--config=x86_64", "--lockfile_mode=error", "--noannounce_rc",
            "--remote_download_outputs=all", "--strategy=TestRunner=local",
            "--nocache_test_results", "--runs_per_test=1", "--flaky_test_attempts=1",
            "--test_output=all", "--test_arg=QualificationReportTest.test_command_completion_preserves_streams_and_failure_evidence",
            owner,
        ], "test", targets=[owner])
        if tested["status"] != "PASS" or len(tested["executed"]) != 1:
            raise RuntimeError("Owning command regression did not pass once")
        attempt = tested["executed"][0]
        if attempt["cached_locally"] or attempt["execution_info"].get("cachedRemotely", False):
            raise ValueError("Owning command regression was cached")
        supplier = config["retained_query"]
        remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Retained-query validation exceeded the work deadline")
        request = urllib.request.Request("https://app.buildbuddy.io/api/v1/GetFile",
            data=json.dumps({"uri": supplier["bundle"]["uri"]}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with urllib.request.urlopen(request, timeout=min(180, remaining)) as response:
            body = response.read(supplier["bundle"]["bytes"] + 1)
        if len(body) != supplier["bundle"]["bytes"] or hashlib.sha256(body).hexdigest() != supplier["bundle"]["sha256"]:
            raise ValueError("Retained query bundle identity differs")
        bundle = private / "retained-query.tar.gz"
        bundle.write_bytes(body)
        directory = evidence / "retained-aquery"
        directory.mkdir()
        with tarfile.open(bundle) as archive:
            for name, identity in supplier["members"].items():
                members = [row for row in archive.getmembers() if row.name == "invocation-bun-compiler-actions/" + name]
                if len(members) != 1 or not members[0].isfile() or Path(name).name != name:
                    raise ValueError("Retained query member is missing, duplicated or not a regular file")
                content = archive.extractfile(members[0]).read()
                if len(content) != identity["bytes"] or hashlib.sha256(content).hexdigest() != identity["sha256"]:
                    raise ValueError("Retained query member identity differs: " + name)
                (directory / name).write_bytes(content)
        original = json.loads((directory / "verdict.json").read_text())
        if (original["source"] != supplier["source"] or original["native_exit"] != 0
                or original["status"] != "INCOMPLETE"
                or original["errors"] != ["Missing or inconsistent original Bazel command"]):
            raise ValueError("Original query failure differs")
        output = directory / "corrected-command.json"
        checked = invoke("retained-aquery-report", [
            "run", "--lockfile_mode=error", "--noannounce_rc", "@rules_python//python/bin:python", "--",
            str(Path(__file__).with_name("qualification_report.py")), "command-verdict",
            "--context", str(directory / "context.json"), "--output", str(output),
        ], "command", targets=[])
        current = json.loads(output.read_text())
        if checked["status"] != "PASS" or current["status"] != "PASS" or current["errors"]:
            raise RuntimeError("Retained action query was not accepted by the corrected reporter")
        for key in ("source", "bazel_command", "arguments", "native_exit", "capture_exit", "command_kind",
                    "command_outputs", "expected", "executed", "case_counts", "bep_finish"):
            if current[key] != original[key]:
                raise ValueError("Retained command identity or execution credit changed: " + key)
        if current["expected"] or current["executed"] or current["case_counts"] or current["bep_finish"] is not None:
            raise ValueError("Action query incorrectly grants build or test credit")
        result.update(status="PASS", original_status=original["status"], corrected_status=current["status"],
                      original_source=original["source"], workload_replays=0,
                      scope="Retained query reporting only; no new aquery, compile, test-consumer or runtime execution")
    finally:
        report.write_json(evidence / "aquery-report-checks.json", result)
