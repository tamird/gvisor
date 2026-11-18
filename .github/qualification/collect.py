"""Require the two intended inline fixture failures in actual BASE XML/BEP."""
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

out = Path(sys.argv[1])
source = Path("bazel-testlogs/runsc/cmd/sentry/forwardcmd/forwardcmd_test")
for name in ("test.xml", "test.log"):
    shutil.copyfile(source / name, out / name)
events = [json.loads(line) for line in (out / "baseline.bep").read_text().splitlines() if line.strip()]
target = "//runsc/cmd/sentry/forwardcmd:forwardcmd_test"
results = [event for event in events if event.get("id", {}).get("testResult", {}).get("label") == target]
summaries = [event for event in events if event.get("id", {}).get("testSummary", {}).get("label") == target]
assert len(results) == len(summaries) == 1, (len(results), len(summaries))
assert results[0]["testResult"]["status"] == "FAILED", results
assert summaries[0]["testSummary"]["overallStatus"] == "FAILED", summaries
assert not any(event.get("testSummary", {}).get("overallStatus") == "FAILED_TO_BUILD" or event.get("testResult", {}).get("status") == "FAILED_TO_BUILD" for event in events)
finished = [event["finished"] for event in events if "finished" in event]
assert len(finished) == 1 and finished[0]["exitCode"]["name"] == "TESTS_FAILED", finished
assert int((out / "baseline-command-exit.txt").read_text()) != 0
root = ET.parse(out / "test.xml").getroot()
cases = list(root.iter("testcase"))
prefix = "TestStatefileDumpMemoryFiles"
leaves = {prefix + "/" + name for name in ("Generic", "CPUWithoutKernel", "Split", "Legacy", "ZeroOwners", "Inline")}
assert len(cases) == 7 and {case.get("name") for case in cases} == leaves | {prefix}
assert not any(case.find("skipped") is not None or case.find("error") is not None or case.get("status") in ("skipped", "notrun") or case.get("result") == "suppressed" for case in cases)
failures = {case.get("name") for case in cases if case.find("failure") is not None}
expected = {prefix + "/ZeroOwners", prefix + "/Inline"}
assert expected <= failures <= expected | {prefix}, failures
for case in cases:
    if case.get("name") in expected:
        # Either writeTo rejects the inline format, or its output differs
        # from the framed generic oracle. Failures outside these boundaries
        # are not an accepted reproduction.
        text = "".join(case.itertext())
        assert "error printing state:" in text or "html=false: got " in text or "html=true: got " in text, text
for suite in root.iter("testsuite"):
    assert int(suite.get("errors", 0)) == int(suite.get("skipped", 0)) == 0
proof = {"status": "expected BASE defect reproduced", "cases": len(cases), "failedLeaves": sorted(expected), "passedLeaves": sorted(leaves - expected), "CIReproduction": False, "scope": "Synthetic inline-format regression at unchanged BASE Statefile.writeTo; no claim that the live CI instance has the same cause."}
(out / "verified-results.json").write_text(json.dumps(proof, indent=2) + "\n")
