"""Verify the ordinary packet test, TCP package, analyzer and layout proofs."""
import json
import os
from pathlib import Path
import re
import sys


def arm(path):
    if not (path / "exit").is_file():
        return {"verified": False, "reason": "missing"}
    bep = path / "bep"
    events = [json.loads(line) for line in bep.read_text().splitlines()] if bep.is_file() else []
    invocations = [e["started"]["uuid"] for e in events if "started" in e]
    complete = {e["id"]["targetCompleted"]["label"] for e in events if "targetCompleted" in e["id"] and e.get("completed", {}).get("success")}
    rc = int((path / "exit").read_text())
    log = (path / "bazel.log").read_text(errors="replace")
    info = {k: (path / k).read_text().strip() for k in ("start", "finish")}
    if path.name == "fixed":
        packet = "//test/packetimpact/tests:tcp_connect_icmp_error_netstack_test"
        tcp = "//pkg/tcpip/transport/tcp:tcp_test"
        nogo = "//pkg/tcpip/transport/tcp:tcp_nogo"
        summaries = {e["id"]["testSummary"]["label"]: e["testSummary"]["overallStatus"] for e in events if "testSummary" in e}
        permanent = "--- PASS: TestTCPEstablishedICMPError" in log
        passed = len(re.findall(r"(?m)^\s*--- PASS: ", log))
        info.update(fixture=(path / "fixture").read_text().split()[0], summaries=summaries, nogo=nogo in complete, packet_case=permanent, passed_rows=passed, skipped_rows=len(re.findall(r"(?m)^\s*--- SKIP: ", log)))
        executions = {e["id"]["testResult"]["label"] for e in events if "testResult" in e["id"] and e.get("testResult", {}).get("status")=="PASSED" and not e["testResult"].get("cachedLocally") and not e["testResult"].get("executionInfo", {}).get("cachedRemotely")}
        info["executed_tests"] = sorted(executions)
        valid = permanent and {packet,tcp} <= executions and nogo in complete and summaries.get(packet)=="PASSED" and summaries.get(tcp)=="PASSED" and set(summaries.values())=={"PASSED"}
    else:
        inputs = [line.split()[0] for line in (path / "inputs").read_text().splitlines()]
        info.update(inputs=inputs, compiler_target="//pkg/tcpip/transport/tcp:tcp" in complete)
        valid = len(inputs)==3 and info["compiler_target"]
    info.update(exit=rc, invocations=invocations, verified=rc==0 and len(invocations)==1 and valid)
    info["diagnostics"]=[line[:480] for line in log.splitlines() if line.startswith(("ERROR:", "--- FAIL:", "FAIL:")) and not any(x in line.lower() for x in ("token", "credential", "header"))][:8]
    return info


if __name__ == "__main__":
    root=Path(sys.argv[1])
    arms={name: arm(root/name) for name in ("fixed","layout-amd64","layout-arm64")}
    ok=all(r["verified"] for r in arms.values()) and arms["layout-amd64"].get("inputs")==arms["layout-arm64"].get("inputs")
    report={"verified":ok, "candidate":os.environ["CANDIDATE"], "baseline_layout":os.environ["STOCK"], "arms":arms}
    (root/"verdict.json").write_text(json.dumps(report,indent=2)+"\n")
    if len(sys.argv)==2:
        raise SystemExit(not ok)
