"""Check the affected musl owner on the current public GNU proposal base."""
import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid
import xml.etree.ElementTree as ET

import plugin_vfio_original as proof
import qualification_report

ROOT = Path(os.environ["RUNNER_TEMP"])
SOURCE = "b0713e2bcf6ccde362401912ae80d17a4e1738b5"
PUBLIC = ROOT / "musl-current-evidence"
PRIVATE = ROOT / "musl-current-private"
BAZEL = ["bazel", "--noblock_for_lock", "--bazelrc=" + str(ROOT / "gnu.bazelrc")]
FLAGS = ["--config=x86_64", "--config=plugin-tldk", "-c", "opt", "--jobs=42",
         "--platforms=@llvm//platforms:linux_x86_64_musl",
         "--extra_toolchains=@llvm//toolchain:linux_x86_64_to_linux_x86_64"]


def save(result):
    (PUBLIC / "result.json").write_text(json.dumps(result, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def run(result):
    assert git("rev-parse", "HEAD") == SOURCE and not git("diff", "HEAD", "--")
    expr = "str(target.label) + '\\t' + str([f.path for f in target.files.to_list()])"
    with (PRIVATE / "selection").open("wb") as out, (PRIVATE / "selection.log").open("wb") as err:
        result["selection_exit"] = subprocess.run([
            "timeout", "10m", *BAZEL, "cquery", *FLAGS, "--output=starlark", "--starlark:expr=" + expr,
            "set(@libpluginstack//:libpluginstack " + proof.TEST + ")"], stdout=out, stderr=err).returncode
    if result["selection_exit"]:
        result["status"] = "FAIL"
    save(result)
    assert result["selection_exit"] == 0
    selected = {}
    for line in (PRIVATE / "selection").read_text().splitlines():
        label, files = line.split("\t")
        label = label[2:] if label.startswith("@@//") else label
        assert label not in selected
        selected[label] = ast.literal_eval(files)
    assert set(selected) == {proof.OWNER, proof.TEST}
    result.update(selected=selected, command_started=proof.now(), invocation=str(uuid.uuid4()))
    save(result)
    with (PRIVATE / "test.log").open("wb") as log:
        result["exit"] = subprocess.run([
            "timeout", "--signal=INT", "--kill-after=30s", "30m", *BAZEL, "test", *FLAGS,
            "--invocation_id=" + result["invocation"], "--build_tag_filters=", "--test_tag_filters=",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--nocache_test_results",
            "--modify_execution_info=^(CcMakeMakeRule|GoLink|TestRunner)$=+no-remote-cache",
            "--test_sharding_strategy=disabled", "--test_timeout=60", "--strategy=TestRunner=remote",
            "--remote_download_outputs=toplevel", "--build_event_json_file_path_conversion=false",
            "--build_event_json_file=" + str(PRIVATE / "raw.bep"),
            "@libpluginstack//:libpluginstack", proof.TEST], stdout=log, stderr=log).returncode
    result.update(command_finished=proof.now(), status="FAIL" if result["exit"] else "INCOMPLETE")
    save(result)
    events = [json.loads(line) for line in (PRIVATE / "raw.bep").read_text().splitlines()]
    result["completed"] = {e["id"]["targetCompleted"]["label"]: e["completed"]["success"]
                           for e in events if "targetCompleted" in e["id"] and "completed" in e}
    finishes = [e["finished"] for e in events if "finished" in e]
    result["finish"] = {k: finishes[0][k] for k in ("overallSuccess", "finishTime", "finishTimeMillis") if k in finishes[0]} if len(finishes) == 1 else {}
    attempts = [e for e in events if e["id"].get("testResult", {}).get("label") == proof.TEST]
    result["attempts"] = [{"id": e["id"]["testResult"], "status": e["testResult"]["status"],
                           "cached": e["testResult"].get("cachedLocally", False) or e["testResult"].get("executionInfo", {}).get("cachedRemotely", False),
                           "strategy": e["testResult"].get("executionInfo", {}).get("strategy"),
                           **{k: e["testResult"][k] for k in ("testAttemptStart", "testAttemptStartMillis", "testAttemptDuration", "testAttemptDurationMillis") if k in e["testResult"]}}
                          for e in attempts]
    save(result)
    capture = qualification_report.capture(PRIVATE / "raw.bep", PRIVATE / "safe.bep", Path("/"), PRIVATE / "reports")
    result["capture_errors"] = capture["errors"]
    reports = [r for r in capture["reports"] if r["name"] == "test.xml" and r["identity"]["label"] == proof.TEST]
    if len(reports) == 1:
        raw = (PRIVATE / reports[0]["path"]).read_bytes()
        result["xml"] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "cases": [
            {"class": c.get("classname"), "name": c.get("name"),
             "status": "FAIL" if c.find("failure") is not None or c.find("error") is not None else
             "SKIP" if c.find("skipped") is not None or c.get("status") == "notrun" or c.get("result") == "suppressed" else "PASS"}
            for c in ET.fromstring(raw).iter("testcase")]}
    save(result)
    expected = [{"class": "backtrace_test", "name": "TestDumpStack", "status": "PASS"}]
    result["runtime_status"] = "PASS" if len(attempts) == 1 and result.get("xml", {}).get("cases") == expected and result["attempts"][0]["status"] == "PASSED" else "INCOMPLETE"
    if any(c["status"] == "FAIL" for c in result.get("xml", {}).get("cases", [])) or any(a["status"] in ("FAILED", "TIMEOUT", "FLAKY") for a in result["attempts"]):
        result["runtime_status"] = result["status"] = "FAIL"
    save(result)
    assert result["exit"] == 0 and result["finish"]["overallSuccess"]
    assert result["completed"] == {proof.OWNER: True, proof.TEST: True}
    attempt, = result["attempts"]
    assert result["runtime_status"] == "PASS" and attempt["strategy"] == "remote" and not attempt["cached"]
    assert not capture["errors"] and len(reports) == 1
    item = {"status": "INCOMPLETE"}
    result["original_action_proof"] = item
    try:
        proof.recover("musl", result, item)
    finally:
        save(result)
    assert item["binary"]["observed_vfio_symbols"] == proof.SYMBOLS
    assert all(not a["cached"] for a in item["actions"])
    assert item["cquery_output_was_actual_link_output"]
    assert git("rev-parse", "HEAD") == SOURCE and not git("diff", "HEAD", "--")
    result["status"] = "PASS"
    try:
        start = datetime.fromisoformat(attempt["testAttemptStart"].replace("Z", "+00:00")) if "testAttemptStart" in attempt else datetime.fromtimestamp(int(attempt["testAttemptStartMillis"]) / 1000, timezone.utc)
        duration = float(attempt["testAttemptDuration"].removesuffix("s")) if "testAttemptDuration" in attempt else int(attempt["testAttemptDurationMillis"]) / 1000
        result["test_completed"] = (start + timedelta(seconds=duration)).isoformat()
    except Exception as error:
        result["timing_error"] = proof.error_info(error)


def main():
    PRIVATE.mkdir(exist_ok=True)
    PUBLIC.mkdir(exist_ok=True)
    result = {"status": "INCOMPLETE", "source": SOURCE, "public_base": "675d49b1c7a1b43ac0d2a5cbe4ab5d897ff44afa",
              "started": proof.now(), "scope": "affected musl archive and static cgo diagnostic on current public GNU base; no runsc/device claim",
              "reused_gnu": {"source": "92c5bed67c", "artifact": 13114392072, "sha256": "4419487585a79dd217e44cd9ffba7af2d874d0fb9af2440f8ef07300c41c6037", "new_execution": False},
              "changed_inputs": ["Linux header default 6.16.12 to 6.18.33", "declared bazel-lib header copy tool"]}
    save(result)
    try:
        run(result)
    except Exception as error:
        result["collection_error"] = proof.error_info(error)
    if result.get("invocation") and result["status"] != "PASS":
        try:
            result["diagnostic"] = proof.diagnostics(result["invocation"])
        except Exception as error:
            result["diagnostic"] = {"status": "INCOMPLETE", "error": proof.error_info(error)}
    result["verified_at"] = proof.now()
    if result.get("test_completed"):
        result["completion_to_verdict_seconds"] = (datetime.fromisoformat(result["verified_at"]) - datetime.fromisoformat(result["test_completed"])).total_seconds()
    save(result)


if __name__ == "__main__":
    main()
