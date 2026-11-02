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
    for row in captured["reports"]:
        if row["name"] != "test.xml":
            continue
        label = row["identity"]["label"]
        case_name = ("FifoTest.HangupDependsOnWhenReaderOpened" if ":fifo_test_" in label
                     else "FcntlLockTest.TestOFDGetLkReturnsNegPID")
        actual = [f"{c.get('classname')}.{c.get('name')}" for c in
                  ET.parse(out / row["path"]).iter("testcase")]
        if actual != [case_name] or label in cases:
            verdict["errors"].append(f"{label}: expected {case_name}; got {actual}")
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
    if set(cases) != targets or sorted(attempts) != sorted(targets) or len(targets) != 6:
        verdict["errors"].append("expected six distinct syscall results and XML reports")
    verdict["cases_by_target"] = cases
    verdict["runner_exit"] = int((out / "runner_exit").read_text())
    if verdict["status"] == "PASS" and (verdict["runner_exit"] or verdict["errors"] or verdict["skips"]):
        verdict["status"] = "FAIL"
    reporting.write_json(Path(destination), verdict)
    print({k: verdict.get(k) for k in ("status", "errors", "missing", "cases_by_target", "invocation")})
    return int(verdict["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
