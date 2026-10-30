"""Verify native runtime workers and both architectures of each owning nogo test."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from statistics import median
import subprocess

tmp = Path(os.environ["RUNNER_TEMP"])
source = "1e9e13978115bcd98a58f6b568c34217ee7dcc35"
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
            package = next(a.removeprefix("-package=") for a in args if a.startswith("-package="))
            checks.append({"label": label, "arch": next((a.split("=", 1)[1] for a in args if a.startswith("-GOARCH=")), None),
                           "sources": [path for path in owners[label] if any(a.endswith(path) for a in args)],
                           "archive": next((a for a in args if a.startswith(f"-archive={package}=")), None), "runner": runner, "exit": code})
    if stage == "runtime":
        row["suite"] = json.loads((tmp / "results/fix.json").read_text())
        assert row["suite"]["qualified"] and len(row["suite"]["bits"]) == 36, row["suite"].get("nan_diagnostics")
    else:
        markers = row["filtered"] = {}
        for label in sorted(analyzers):
            package, name = label[2:].split(":")
            contents = (tmp / "testlogs" / package / name / "test.log").read_text()
            if row["targets"].get(label) != "PASSED":
                row.setdefault("test_diagnostics", {})[label] = contents[-1200:]
            markers[label] = sorted(re.findall(r"(amd64|arm64)\.\.\.PASS", contents))
        assert all(archs == ["amd64", "arm64"] for archs in markers.values()), markers
        focused = (tmp / "testlogs/pkg/state/tests/tests_test/test.log").read_text()
        row["amd64_signaling_nan"] = "--- PASS: TestSignalingNaN" in focused
        assert row["amd64_signaling_nan"]
        verified = {(c["label"], c["arch"]) for c in checks if c["exit"] == 0 and c["archive"] and len(c["sources"]) == len(owners[c["label"]])}
        assert verified == {(label, arch) for label in owners for arch in ("amd64", "arm64")}, checks
    expected_arch = "arm64" if stage == "runtime" else "amd64"
    assert {t["label"] for t in traces} == labels and all(
        t["runner"] == "remote" and t["arch"] == expected_arch and t["action"] and not t["hit"] and t["exit"] == 0
        for t in traces), traces
    assert row["exit"] == 0 and row["targets"] == dict.fromkeys(labels, "PASSED"), row["targets"]


for stage, labels in (("runtime", tests), ("nogo", analyzers | {"//pkg/state/tests:tests_test"})):
    row = receipt["stages"][stage] = {"expected": sorted(labels)}
    try:
        capture(stage, labels, row)
    except Exception as error:
        receipt["errors"].append(f"{stage}: {type(error).__name__}: {error}"[:1100])

if (tmp / "native-actions.txt").exists():
    receipt["preflight_execution_platforms"] = sorted(set(re.findall(
        r"^  Execution platform: (.+)$", (tmp / "native-actions.txt").read_text(), re.M)))
receipt["diagnostic_runs"] = [40999583492, 41000054047]
try:
    benchmarks = receipt["finite_arrays"] = {}
    for block, group in enumerate(("previous", "candidate", "candidate", "previous"), 1):
        row = benchmarks.setdefault(group, {})
        assert int((tmp / f"bench{block}.exit").read_text()) == 0
        matches = re.findall(r"^BenchmarkLegacyScalarArray/(\S+)-\d+\s+\d+\s+([\d.]+) ns/op\s+(\d+) B/op\s+(\d+) allocs/op",
                             (tmp / f"bench{block}.txt").read_text(), re.M)
        assert len(matches) == 16, (block, len(matches))
        for name, runtime, mem, allocs in matches:
            row.setdefault(name, []).append((float(runtime), int(mem), int(allocs)))
    for group in benchmarks:
        assert len(benchmarks[group]) == 4
        for name, values in benchmarks[group].items():
            assert len(values) == 8
            benchmarks[group][name] = {"samples": len(values), "median_ns": median(v[0] for v in values),
                                       "bytes": sorted({v[1] for v in values}), "allocs": sorted({v[2] for v in values})}
    for name, result in benchmarks["previous"].items():
        for field in ("bytes", "allocs"):
            assert result[field] == benchmarks["candidate"][name][field], (name, field)
except Exception as error:
    receipt["errors"].append(f"finite arrays: {type(error).__name__}: {error}"[:1100])
receipt["qualified"] = not receipt["errors"]
receipt["verified"] = datetime.now(timezone.utc).isoformat()
(tmp / "results/arm.json").write_text(json.dumps(receipt, indent=2) + "\n")
print({stage: {"exit": row.get("exit"), "missing": row.get("missing")} for stage, row in receipt["stages"].items()}, receipt["errors"])
raise SystemExit(0 if receipt["qualified"] else 1)
