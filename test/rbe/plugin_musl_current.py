"""Exercise the opt-in plugin test using the two supported Linux libcs."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid
import xml.etree.ElementTree as ET

import qualification_report

ROOT = Path(os.environ["RUNNER_TEMP"])
SOURCE = os.environ["PRODUCT"]
TEST = "@@+http_archive+libpluginstack//:backtrace_test"
SELECTION_SOURCE = "ddd06cb4a7d004178513c25c018ede20d8cc9449"
SELECTION_INVOCATIONS = {"gnu": "aef50e3a-212a-41ca-9a1d-fa778ddc159f",
                         "musl": "a9fc04b4-fffd-491c-8dfb-efefffd7a2c6"}
PUBLIC_TEST = "@libpluginstack//:backtrace_test"

PUBLIC = ROOT / "musl-current-evidence"
BAZEL = [str(ROOT / "bazel-9.3.1"), "--noblock_for_lock", "--bazelrc=" + str(ROOT / "gnu.bazelrc")]
FLAGS = ["--config=x86_64", "--config=plugin-tldk", "--strip=never", "-c", "opt", "--jobs=42"]
MUSL = ["--platforms=@llvm//platforms:linux_x86_64_musl",
        "--extra_toolchains=@llvm//toolchain:linux_x86_64_to_linux_x86_64"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save(result):
    (PUBLIC / "result.json").write_text(json.dumps(result, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def run(profile: str, result: dict) -> None:
    private = ROOT / f"tldk-private-{profile}"
    private.mkdir(exist_ok=True)
    flags = FLAGS + (MUSL if profile == "musl" else [])
    assert git("rev-parse", "HEAD") == SOURCE and not git("diff", "HEAD", "--")
    assert SOURCE == SELECTION_SOURCE, "selection receipt is for different source"
    result.update(selection_invocation=SELECTION_INVOCATIONS[profile],
                  command_started=now(), invocation=str(uuid.uuid4()))
    with (private / "test.log").open("wb") as log:
        result["exit"] = subprocess.run([
            "timeout", "--signal=INT", "--kill-after=30s", "30m", *BAZEL, "test", *flags,
            "--invocation_id=" + result["invocation"], "--build_tag_filters=", "--test_tag_filters=",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--nocache_test_results",
            "--test_arg=-test.v",
            "--test_sharding_strategy=disabled", "--test_timeout=60", "--strategy=TestRunner=remote",
            "--remote_download_outputs=toplevel", "--build_event_json_file_path_conversion=false",
            "--build_event_json_file=" + str(private / "raw.bep"),
            PUBLIC_TEST], stdout=log, stderr=log).returncode
    result.update(command_finished=now(), status="FAIL" if result["exit"] else "INCOMPLETE")
    events = [json.loads(line) for line in (private / "raw.bep").read_text().splitlines()]
    result["completed"] = {e["id"]["targetCompleted"]["label"]: e["completed"].get("success", False)
                           for e in events if "targetCompleted" in e["id"] and "completed" in e}
    finishes = [e["finished"] for e in events if "finished" in e]
    result["finish"] = {k: finishes[0][k] for k in ("overallSuccess", "finishTime", "finishTimeMillis") if k in finishes[0]} if len(finishes) == 1 else {}
    attempts = [e for e in events if "testResult" in e["id"]]
    result["attempt_labels"] = [e["id"]["testResult"]["label"] for e in attempts]
    assert all(label in (TEST, PUBLIC_TEST) for label in result["attempt_labels"]), result["attempt_labels"]
    result["attempts"] = [{"id": e["id"]["testResult"], "status": e["testResult"]["status"],
                           "cached": e["testResult"].get("cachedLocally", False) or e["testResult"].get("executionInfo", {}).get("cachedRemotely", False),
                           "strategy": e["testResult"].get("executionInfo", {}).get("strategy"),
                           **{k: e["testResult"][k] for k in ("testAttemptStart", "testAttemptStartMillis", "testAttemptDuration", "testAttemptDurationMillis") if k in e["testResult"]}}
                          for e in attempts]
    capture = qualification_report.capture(private / "raw.bep", private / "safe.bep", Path("/"), private / "reports")
    result["capture_errors"] = capture["errors"]
    reports = [r for r in capture["reports"] if r["name"] == "test.xml" and r["identity"]["label"] in (TEST, PUBLIC_TEST)]
    if len(reports) == 1:
        raw = (private / reports[0]["path"]).read_bytes()
        result["xml"] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "cases": [
            {"class": c.get("classname"), "name": c.get("name"),
             "status": "FAIL" if c.find("failure") is not None or c.find("error") is not None else
             "SKIP" if c.find("skipped") is not None or c.get("status") == "notrun" or c.get("result") == "suppressed" else "PASS"}
            for c in ET.fromstring(raw).iter("testcase")]}
    expected = [("TestDumpStack", "PASS")]
    result["runtime_status"] = "PASS" if len(attempts) == 1 and [(c["name"], c["status"]) for c in result.get("xml", {}).get("cases", [])] == expected and result["attempts"][0]["status"] == "PASSED" else "INCOMPLETE"
    if any(c["status"] == "FAIL" for c in result.get("xml", {}).get("cases", [])) or any(a["status"] in ("FAILED", "TIMEOUT", "FLAKY") for a in result["attempts"]):
        result["runtime_status"] = result["status"] = "FAIL"
    assert result["exit"] == 0 and result["finish"]["overallSuccess"]
    assert any(result["completed"].get(label) is True for label in (TEST, PUBLIC_TEST)), result["completed"]
    attempt, = result["attempts"]
    assert result["runtime_status"] == "PASS" and attempt["strategy"] == "remote" and not attempt["cached"]
    assert not capture["errors"] and len(reports) == 1
    assert git("rev-parse", "HEAD") == SOURCE and not git("diff", "HEAD", "--")
    result["status"] = "PASS"
    try:
        start = datetime.fromisoformat(attempt["testAttemptStart"].replace("Z", "+00:00")) if "testAttemptStart" in attempt else datetime.fromtimestamp(int(attempt["testAttemptStartMillis"]) / 1000, timezone.utc)
        duration = float(attempt["testAttemptDuration"].removesuffix("s")) if "testAttemptDuration" in attempt else int(attempt["testAttemptDurationMillis"]) / 1000
        result["test_completed"] = (start + timedelta(seconds=duration)).isoformat()
    except Exception as error:
        result["timing_error"] = type(error).__name__


def main() -> None:
    PUBLIC.mkdir(exist_ok=True)
    result = {"source": SOURCE, "baseline": "93993d354e440e71b7e6efaacd72be40e542de57",
              "selection_artifact": 13440938628,
              "selection_archive_sha256": "6038279aba5df8464d02994a0968a5f08b50418446c7cbd47bc52ad5919810f5",
              "status": "INCOMPLETE", "started": now(), "profiles": {},
              "scope": "two opt-in TestDumpStack executions, no runsc or VFIO device test"}
    save(result)
    assert Path(".bazelversion").read_text().strip() == "9.3.1"
    for profile in ("gnu", "musl"):
        item = {"status": "INCOMPLETE", "started": now()}
        result["profiles"][profile] = item
        try:
            run(profile, item)
        except Exception as error:
            item["failure_location"] = error.__traceback__.tb_next.tb_lineno if error.__traceback__.tb_next else error.__traceback__.tb_lineno
            item["error"] = type(error).__name__
        item["verified_at"] = now()
        save(result)
    result["status"] = "PASS" if all(item["status"] == "PASS" for item in result["profiles"].values()) else "FAIL"
    result["verified_at"] = now()
    save(result)


if __name__ == "__main__":
    main()
