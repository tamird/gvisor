"""Record the exact source locations flagged by the semaphore Nogo target."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

root = Path(os.environ["RUNNER_TEMP"]) / "sem-borrow"
phase = sys.argv[1]
assert phase in ("original", "probe"), phase
case_types = ("diagnostic", "valid", "wrong owner", "unguarded", "lifetime")
fixture = Path("pkg/sentry/kernel/semaphore/semaphore.go")
owner = "//pkg/sentry/kernel/semaphore:semaphore_nogo"
source = os.environ["SOURCE"] if phase == "original" else os.environ["PROBE"]
report = {"source": source, "exit": int((root / (phase + ".exit")).read_text()), "verified": False}
try:
    cases = {}
    if phase == "probe":
        for line, content in enumerate(fixture.read_text().splitlines(), 1):
            marker = content.partition("// ")[2]
            kind, _, detail = marker.partition(": ")
            if kind in case_types:
                cases[line] = {"kind": kind, "case": detail}
        assert len(cases) == 11, cases
    results = []
    for line in (root / (phase + ".bep")).read_bytes().splitlines():
        event = json.loads(line)
        if "started" in event:
            report["invocation"] = str(uuid.UUID(event["started"]["uuid"]))
        if "testResult" in event and event["id"]["testResult"]["label"] == owner:
            results.append(event["testResult"])
    result, = results
    info = result.get("executionInfo", {})
    report["status"] = result["status"]
    report["strategy"] = info.get("strategy")
    report["cached"] = bool(result.get("cachedLocally") or info.get("cachedRemotely"))
    assert report["strategy"] == "remote" and not report["cached"], report
    data = Path("bazel-testlogs/pkg/sentry/kernel/semaphore/semaphore_nogo/test.log").read_bytes()
    report["log_sha256"] = hashlib.sha256(data).hexdigest()
    sections = re.split(r"(?m)^(amd64|arm64)\.\.\.", data.decode())
    by_arch = dict(zip(sections[1::2], sections[2::2]))
    assert len(sections) == 5 and set(by_arch) == {"amd64", "arm64"}, by_arch.keys()
    report["cases"] = cases
    report["architecture"] = {}
    for arch, body in by_arch.items():
        messages = [line.strip() for line in body.splitlines() if "checklocks:" in line]
        findings = []
        for message in messages:
            match = re.search(r"semaphore\.go:(\d+):\d+: (.*) \(GOO?ARCH=", message)
            assert match, message[:150]
            position = int(match[1])
            detail = match[2].partition(" (locks:")[0]
            findings.append({"line": position, "source": fixture.read_text().splitlines()[position-1].strip(), "detail": detail})
        assert any("borrow probe: possible intervening release" in f["detail"] for f in findings)
        assert any("borrow probe: unproven callee" in f["detail"] for f in findings)
        assert any("borrow probe: stored pointer" in f["detail"] for f in findings)
        report["architecture"][arch] = findings
    assert report["architecture"]["amd64"] == report["architecture"]["arm64"]
    assert (report["exit"], report["status"]) == (3, "FAILED"), report
    report["verified"] = True
finally:
    (root / (phase + ".json")).write_text(json.dumps(report, indent=2)+"\n")
