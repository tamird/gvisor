"""Check original GTest reports for the two requested syscall cases."""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import qualification_report as reporting


def main():
    out, source, destination = sys.argv[1:]
    out = Path(out)
    events = out / "events.jsonl"
    captured = reporting.capture(out / "raw.jsonl", events, Path("bazel-out"), out / "reports")
    (out / "capture_exit").write_text(str(int(bool(captured["errors"]))))
    context = {key: key for key in ("commit_file", "source_before", "source_after", "final_head",
                                     "executed_arguments", "native_exit", "capture_exit")}
    context.update(expected_source=source, command_kind="test",
                   execution_finished_at=(out / "finished").read_text().strip())
    reporting.write_json(out / "context.json", context)
    verdict = reporting.verdict(events, reporting.manifest_path(events), out / "context.json",
                                out / "targets.txt", ())
    targets = set((out / "targets.txt").read_text().splitlines())
    cases = {}
    restore_count = 0
    for row in captured["reports"]:
        label = row["identity"]["label"]
        if row["name"] == "test.log" and label.endswith("_save"):
            with (out / row["path"]).open("rb") as log:
                restore_count += sum(b"Executing:" in line and b" restore " in line and b"--image-path" in line for line in log)
        if row["name"] != "test.xml":
            continue
        xml = ET.parse(out / row["path"])
        actual = [f"{c.get('classname')}.{c.get('name')}" for c in xml.iter("testcase")]
        if label == "//pkg/sentry/fsimpl/lock:lock_test":
            match = any(c.get("name") == "TestFileLocksFirstUse" for c in xml.iter("testcase"))
        elif ":fifo_test_" in label:
            match = actual == ["FifoTest.HangupDependsOnWhenReaderOpened"]
        else:
            match = sorted(actual) == ["FcntlLockTest.GetLockWithMixedReaders", "FcntlLockTest.TestOFDGetLkReturnsNegPID"]
        if not match or label in cases:
            verdict["errors"].append(f"{label}: wrong or repeated named results: {actual}")
        cases[label] = actual
    attempts = []
    for line in events.read_text().splitlines():
        event = json.loads(line)
        if "started" in event:
            verdict["invocation"] = event["started"]["uuid"]
        if "testResult" not in event:
            continue
        result = event["testResult"]
        label = event["id"]["testResult"]["label"]
        attempts.append(label)
        info = result.get("executionInfo", {})
        if result.get("cachedLocally") or info.get("cachedRemotely") or info.get("strategy") != "remote":
            verdict["errors"].append(f"{label}: expected uncached remote execution")
    locking = "//pkg/sentry/fsimpl/lock:lock_test" in targets
    if set(cases) != targets or sorted(attempts) != sorted(targets) or len(targets) != (5 if locking else 3):
        verdict["errors"].append("missing or duplicate result or XML")
    if locking and restore_count == 0:
        verdict["errors"].append("no actual restore invocation in the forced-save test log")
    verdict["restore_count"] = restore_count
    verdict["cases_by_target"] = cases
    verdict["runner_exit"] = int((out / "runner_exit").read_text())
    if verdict["status"] == "PASS" and (verdict["runner_exit"] or verdict["errors"] or verdict["skips"]):
        verdict["status"] = "FAIL"
    reporting.write_json(Path(destination), verdict)
    print({k: verdict.get(k) for k in ("status", "errors", "missing", "cases_by_target", "invocation")})
    return int(verdict["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
