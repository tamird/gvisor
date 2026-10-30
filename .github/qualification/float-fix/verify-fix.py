from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "results"
out.mkdir(exist_ok=True)
phase = sys.argv[1]
targets = {"//pkg/state:state_test": "pkg/state/state_test", "//pkg/state/tests:tests_test": "pkg/state/tests/tests_test"}
if phase == "finish":
    rows = [json.loads((out / f"{arm}.json").read_text()) for arm in ("stock", "fix")]
    names = {f"BenchmarkLegacyScalarArray/{typ}/{op}" for typ in ("Float32", "Complex64") for op in ("Save", "Load")}
    checks = []
    for i in range(1, 5):
        trace = (out / f"bench{i}.txt").read_text()
        hits = re.findall(r"^(BenchmarkLegacyScalarArray/[^\s-]+)-[0-9]+\s+\d+\s+[0-9.]+ ns/op", trace, re.M)
        checks.append(len(hits) == 16 and set(hits) == names)
    verdict = {"source": os.environ["GITHUB_SHA"], "stock": rows[0], "fixed": rows[1], "complete_bench_blocks": checks, "verified": datetime.now(timezone.utc).isoformat()}
    passed = all(checks) and all(v["qualified"] for v in rows)
    verdict["qualified"] = passed
    (out / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    sys.exit(not passed)

arm, code, head = phase, int(sys.argv[2]), sys.argv[3]
active = list(targets) if arm == "fix" else ["//pkg/state/tests:tests_test"]
events = [json.loads(v) for v in (tmp / f"{arm}.jsonl").read_text().splitlines()]
summary = {v["id"]["testSummary"]["label"]: v["testSummary"]["overallStatus"] for v in events if "testSummary" in v}
records, logs, failures, skips = {}, {}, [], []
for label in active:
    d = Path("bazel-testlogs") / targets[label]
    logs[label] = (d / "test.log").read_text() if (d / "test.log").exists() else ""
    cases = ET.parse(d / "test.xml").findall(".//testcase") if (d / "test.xml").exists() else []
    records[label] = len(cases)
    failures.extend(c.attrib.get("name", "") for c in cases if c.find("failure") is not None or c.find("error") is not None)
    skips.extend(c.attrib.get("name", "") for c in cases if c.find("skipped") is not None or c.get("status") == "notrun")
suite = logs["//pkg/state/tests:tests_test"]
markers = re.findall(r"(sample [01]|real|imaginary): got (0x[0-9a-f]+), want (0x[0-9a-f]+)", suite)
if arm == "stock":
    ok = code == 3 and summary == {active[0]: "FAILED"} and bool(failures) and set((s, int(x,16), int(y,16)) for s,x,y in markers) == {("sample 0", 0x7fc01234, 0x7f801234), ("sample 1", 0xffc01234, 0xff801234), ("real", 0xffc01234, 0xff801234), ("imaginary", 0x7fc01234, 0x7f801234)}
    bits = []
else:
    data = logs["//pkg/state:state_test"]
    bits = re.findall(r"BITS (legacy|graph) (scalar|complex|slice) ([0-9a-f]{8}) ([0-9a-f]{8})", data)
    if not bits:
        xml = (Path("bazel-testlogs") / targets["//pkg/state:state_test"] / "test.xml").read_text()
        bits = re.findall(r"BITS (legacy|graph) (scalar|complex|slice) ([0-9a-f]{8}) ([0-9a-f]{8})", xml)
    mismatches = sorted((e,f,x,y) for e,f,x,y in bits if x != y)
    ok = code == 0 and not failures and len(set(bits)) == len(bits) == 36 and summary == dict.fromkeys(active, "PASSED") and all(records.values()) and mismatches == [("legacy","scalar","80000000","00000000"), ("legacy","slice","80000000","00000000")] and "--- PASS: TestSignalingNaN" in suite
receipt = {"source": head, "targets": active, "summary": summary, "missing": sorted(set(active) - set(summary)), "records": records, "code": code, "failures": failures, "skips": skips, "nan_diagnostics": markers, "bits": bits, "qualified": ok, "finish": [v["finished"].get("finishTime") for v in events if "finished" in v], "verified": datetime.now(timezone.utc).isoformat()}
(out / f"{arm}.json").write_text(json.dumps(receipt, indent=2) + "\n")
