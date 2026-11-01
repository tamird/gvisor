"""Require each original native/stock/fixed packet outcome and both ARM analyzers."""
import json
import os
from pathlib import Path
import sys


def arm(path):
    if not (path / "exit").is_file():
        return {"verified": False, "reason": "missing"}
    events = [json.loads(line) for line in (path / "bep").read_text().splitlines()] if (path / "bep").is_file() else []
    invocations = [e["started"]["uuid"] for e in events if "started" in e]
    rc = int((path / "exit").read_text())
    text = (path / "bazel.log").read_text(errors="replace")
    evidence = {key: (path / key).read_text().strip() for key in ("start", "finish")}
    if path.name == "arm-analysis":
        labels = {"//pkg/tcpip/transport/tcp:tcp_nogo", "//pkg/tcpip/transport/tcp/test/e2e:tcp_test_nogo"}
        complete = {e["id"]["targetCompleted"]["label"] for e in events if e.get("completed", {}).get("success") and "targetCompleted" in e.get("id", {})}
        evidence["completed"] = sorted(labels & complete)
        ok = rc == 0 and labels <= complete
    else:
        label = "//test/packetimpact/tests:tcp_connect_icmp_error_" + ("native" if path.name == "native" else "netstack") + "_test"
        summaries = {e["id"]["testSummary"]["label"]: e["testSummary"]["overallStatus"] for e in events if "testSummary" in e}
        original_failure = "SO_ERROR for acknowledged data: no route to host" in text
        passed = "--- PASS: TestTCPEstablishedICMPError" in text
        evidence.update(fixture=(path / "fixture").read_text().split()[0], summaries=summaries, stale_error=original_failure, case_passed=passed)
        if path.name == "stock":
            ok = rc != 0 and summaries == {label: "FAILED"} and original_failure and not passed
        else:
            ok = rc == 0 and summaries == {label: "PASSED"} and passed and not original_failure
    evidence.update(exit=rc, verified=ok and len(invocations)==1, invocations=invocations)
    evidence["diagnostics"] = [line[:320] for line in text.splitlines() if ("SO_ERROR for " in line or line.startswith(("ERROR:", "--- FAIL:", "FAIL:"))) and not any(x in line.lower() for x in ("credential", "token", "remote_header", "bes_header"))][:8]
    return evidence


if __name__ == "__main__":
    root = Path(sys.argv[1])
    rows = {stage: arm(root / stage) for stage in ("native", "stock", "fixed", "arm-analysis")}
    product = all(rows[s]["verified"] for s in ("native", "stock", "fixed")) and len({rows[s].get("fixture") for s in ("native", "stock", "fixed")}) == 1
    ok = product and rows["arm-analysis"]["verified"]
    report = {"verified":ok, "packets_verified":product, "candidate":os.environ["CANDIDATE"], "terminal_analysis_source":os.environ["TERMINAL"], "arms":rows}
    (root / "verdict.json").write_text(json.dumps(report,indent=2)+"\n")
    if len(sys.argv)==2:
        raise SystemExit(not ok)
