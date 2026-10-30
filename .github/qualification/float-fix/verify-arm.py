"""Verify native runtime workers and both architectures of each owning nogo test."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess

tmp = Path(os.environ["RUNNER_TEMP"])
source = "0e2d23ed2e6d60a724e0da199bed111050a094c5"
tests = {"//pkg/state:state_test", "//pkg/state/tests:tests_test"}
owners = {
    "//pkg/state:state": ("pkg/state/encode.go", "pkg/state/decode.go", "pkg/state/types.go"),
    "//pkg/state/wire:wire": ("pkg/state/wire/wire.go", "pkg/state/wire/load.go"),
    "//pkg/state/tests:tests_test": ("pkg/state/tests/float_test.go",),
}
analyzers = {name + "_nogo" for name in owners}
receipt = {"source": source, "fixture": os.environ["GITHUB_SHA"], "state_tree": (tmp / "state-tree").read_text().strip(),
           "stages": {}, "errors": []}


def capture(stage, labels, row):
    row["exit"] = int((tmp / (stage + ".exit")).read_text())
    events = tmp / ("fix.jsonl" if stage == "runtime" else "nogo.jsonl")
    events = [json.loads(line) for line in events.read_text().splitlines()]
    starts = [e["started"] for e in events if "started" in e]
    finishes = [e["finished"] for e in events if "finished" in e]
    if starts:
        row.update(invocation=starts[0]["uuid"], started=starts[0].get("startTime"))
    if finishes:
        row["finished"] = finishes[-1].get("finishTime")
    row["targets"] = {e["id"]["testSummary"]["label"]: e["testSummary"]["overallStatus"] for e in events if "testSummary" in e}
    row["missing"] = sorted(labels - row["targets"].keys())
    row["diagnostics"] = [s[:500] for s in (tmp / (stage + ".log")).read_text(errors="replace").splitlines() if s.startswith("ERROR:")][:8]
    log_path = tmp / (stage + ".binpb")
    raw = subprocess.check_output([tmp / "bb", "print", f"--compact_execution_log={log_path}", "--raw=true", "--sort=false"], text=True)
    decoder = json.JSONDecoder()
    pos = 0
    traces = row["test_actions"] = []
    checks = row["package_checks"] = []
    while pos < len(raw):
        if raw[pos].isspace():
            pos += 1
            continue
        record, pos = decoder.raw_decode(raw, pos)
        action = record.get("spawn", {})
        label = action.get("targetLabel")
        args = action.get("args", [])
        runner = action.get("runner")
        code = action.get("exitCode", 0)
        if action.get("mnemonic") == "TestRunner" and label in labels:
            platform = {p["name"]: p["value"] for p in action.get("platform", {}).get("properties", [])}
            traces.append({"label": label, "runner": runner, "arch": platform.get("Arch"), "image": platform.get("container-image"),
                           "exit": code, "hit": action.get("cacheHit", False), "action": action.get("digest")})
        if stage == "nogo" and action.get("mnemonic") == "GoStaticAnalysis" and label in owners and "check" in args:
            checks.append({"label": label, "arch": next((a.split("=", 1)[1] for a in args if a.startswith("-GOARCH=")), None),
                           "sources": [path for path in owners[label] if any(a.endswith(path) for a in args)],
                           "archive": next((a for a in args if a.startswith("-archive=")), None), "runner": runner, "exit": code})
    if stage == "runtime":
        row["suite"] = json.loads((tmp / "results/fix.json").read_text())
        assert row["suite"]["qualified"] and len(row["suite"]["bits"]) == 36, row["suite"].get("nan_diagnostics")
    else:
        markers = row["filtered"] = {}
        for label in sorted(labels):
            package, name = label[2:].split(":")
            contents = (Path("bazel-testlogs") / package / name / "test.log").read_text()
            if row["targets"].get(label) != "PASSED":
                row.setdefault("test_diagnostics", {})[label] = contents[-1200:]
            markers[label] = sorted(re.findall(r"(amd64|arm64)\.\.\.PASS", contents))
        assert all(archs == ["amd64", "arm64"] for archs in markers.values()), markers
        verified = {(c["label"], c["arch"]) for c in checks if c["exit"] == 0 and c["archive"] and len(c["sources"]) == len(owners[c["label"]])}
        assert verified == {(label, arch) for label in owners for arch in ("amd64", "arm64")}, checks
    expected_arch = "arm64" if stage == "runtime" else "amd64"
    assert {t["label"] for t in traces} == labels and all(
        t["runner"] == "remote" and t["arch"] == expected_arch and t["action"] and not t["hit"] and t["exit"] == 0
        for t in traces), traces
    assert row["exit"] == 0 and row["targets"] == dict.fromkeys(labels, "PASSED"), row["targets"]


for stage, labels in (("runtime", tests), ("nogo", analyzers)):
    row = receipt["stages"][stage] = {"expected": sorted(labels)}
    try:
        capture(stage, labels, row)
    except Exception as error:
        receipt["errors"].append(f"{stage}: {type(error).__name__}: {error}"[:1100])

receipt["qualified"] = not receipt["errors"]
receipt["verified"] = datetime.now(timezone.utc).isoformat()
(tmp / "results/arm.json").write_text(json.dumps(receipt, indent=2) + "\n")
print({stage: {"exit": row.get("exit"), "missing": row.get("missing")} for stage, row in receipt["stages"].items()}, receipt["errors"])
raise SystemExit(0 if receipt["qualified"] else 1)
