"""Check the original checker and generated-package analysis results."""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import qualification_report as reporting


def main():
    out, source, destination, phase = sys.argv[1:]
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
        if not actual or label in cases:
            verdict["errors"].append(f"{label}: missing or repeated named results")
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
    if phase == "public":
        required_targets = {"//tools/nogo/check:check_test_nogo", "//pkg/eventchannel:eventchannel_nogo", "//pkg/sync:sync_nogo"}
        actions_path = out / "actions.json"
        rendered = []
        if actions_path.exists() and actions_path.stat().st_size:
            for action in json.loads(actions_path.read_text())["actions"]:
                args = action.get("arguments", [])
                if "render" in args and any(arg.startswith("-output=") and arg.endswith("/runtime_constants_go_impl.go") for arg in args):
                    rendered.extend(arg.removeprefix("-GOARCH=") for arg in args if arg.startswith("-GOARCH="))
        verdict["sync_constants_render"] = sorted(rendered)
        if sorted(set(rendered)) != ["amd64", "arm64"]:
            verdict["errors"].append("missing runtime_constants_go_render action for both architectures")
    elif phase == "legacy":
        required_targets = {"//tools/nogo/qualification_legacy:legacy_test"}
        actions_path = out / "actions.json"
        observed = []
        if actions_path.exists() and actions_path.stat().st_size:
            actions = json.loads(actions_path.read_text())["actions"]
            prefix = "gvisor.dev/gvisor/tools/nogo/qualification_legacy/"
            for action in actions:
                args = action.get("arguments", [])
                if f"-package={prefix}consumer" not in args:
                    continue
                inputs = {arg.removeprefix("-compiler-import=").split("=", 1)[0]:
                          Path(arg.rsplit("=", 1)[-1]).suffix
                          for arg in args if arg.startswith("-compiler-import=")}
                arch, = [arg.removeprefix("-GOARCH=") for arg in args if arg.startswith("-GOARCH=")]
                observed.append({"arch": arch, "inputs": inputs})
        verdict["archive_only_actions"] = observed
        if sorted(r["arch"] for r in observed) != ["amd64", "arm64"] or any(
            r["inputs"] != {prefix + "own": ".x", prefix + "extra": ".x"} for r in observed
        ):
            verdict["errors"].append("missing own/hook archive inputs for both consumer configurations")
    else:
        raise ValueError(phase)
    if targets != required_targets or set(cases) != targets or sorted(attempts) != sorted(targets):
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
