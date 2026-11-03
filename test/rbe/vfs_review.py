"""Check the original VFS package test results."""

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
        xml = ET.parse(out / row["path"])
        actual = [f"{c.get('classname')}.{c.get('name')}" for c in xml.iter("testcase")]
        match = any(c.get("name") == "TestMountTableLookupEmpty" for c in xml.iter("testcase"))
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
    if targets != {"//pkg/sentry/vfs:vfs_test"} or set(cases) != targets or sorted(attempts) != sorted(targets):
        verdict["errors"].append("missing or duplicate result or XML")
    verdict["cases_by_target"] = cases
    verdict["runner_exit"] = int((out / "runner_exit").read_text())
    if verdict["status"] == "PASS" and (verdict["runner_exit"] or verdict["errors"] or verdict["skips"]):
        verdict["status"] = "FAIL"
    reporting.write_json(Path(destination), verdict)
    print({k: verdict.get(k) for k in ("status", "errors", "missing", "cases_by_target", "invocation")})
    return int(verdict["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
