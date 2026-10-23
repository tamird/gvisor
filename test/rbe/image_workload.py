"""Validate retained image projection without repeating image or runtime work."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import time
import urllib.request

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["targets"] or config["runtime"]:
        raise ValueError("Projection controls must not select image or runtime work")
    scripts = Path(__file__).parent
    fixture = json.loads((scripts / "image_result_controls.json").read_text())
    result = {"source": config["source"], "status": "INCOMPLETE", "workload_replays": 0}
    bundles = private / "retained-image-bundles"
    bundles.mkdir()
    projected = private / "projected-images"
    projected.mkdir()
    try:
        for case in ("pass", "fail"):
            supplier = fixture[case]["bundle"]
            remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Retained image validation exhausted the work deadline")
            request = urllib.request.Request("https://app.buildbuddy.io/api/v1/GetFile",
                data=json.dumps({"uri": supplier["uri"]}).encode(),
                headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
            with urllib.request.urlopen(request, timeout=min(180, remaining)) as response:
                body = response.read(supplier["bytes"] + 1)
            if (len(body), hashlib.sha256(body).hexdigest()) != (supplier["bytes"], supplier["sha256"]):
                raise ValueError("Retained bundle identity differs: " + case)
            (bundles / (case + ".tar.gz")).write_bytes(body)
        run_under = shlex.join(["env", "PYTHONPATH=" + str(Path(report.__file__).parent)])
        checked = invoke("retained-image-results", [
            "run", "--lockfile_mode=error", "--noannounce_rc", "--run_under=" + run_under,
            "@rules_python//python/bin:python", "--", str(scripts / "image_result_controls.py"),
            str(bundles), str(projected),
        ], "command", targets=[])
        names = ["test_retained_pass_preserves_all_image_and_runtime_joins",
                 "test_retained_native_failure_remains_failure",
                 "test_missing_bundle_writes_incomplete_and_exits_nonzero"]
        log = (evidence / "invocation-retained-image-results/stderr.log").read_text()
        if checked["status"] != "PASS" or "Ran 3 tests" not in log:
            raise RuntimeError("Retained image controls did not execute successfully")
        for name in names:
            if not any(line.startswith(name + " (") and line.endswith(" ... ok") for line in log.splitlines()):
                raise ValueError("Missing positive owning control: " + name)
        results = {}
        for name in names:
            path = projected / name / "retained-outcome.json"
            outcome = json.loads(path.read_text())
            destination = evidence / (name + "-outcome.json")
            destination.write_bytes(path.read_bytes())
            results[name] = {"status": outcome["status"], "native_status": outcome["native_status"],
                             "projection_status": outcome["projection_status"], **report.file_record(destination)}
        result.update(status="PASS", controls=results, reporter=report.file_record(Path(report.__file__)),
                      scope="Three actual Python controls over two retained bundles; no image build, runtime or original test replay")
    finally:
        report.write_json(evidence / "image-result-controls.json", result)
