"""Require the IPv6 network control and selected address-unreachable outcome."""
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
    label = "//test/packetimpact/tests:tcp_network_unreachable_" + ("native" if path.name == "native" else "netstack") + "_test"
    summaries = {e["id"]["testSummary"]["label"]: e["testSummary"]["overallStatus"] for e in events if "testSummary" in e}
    present = {e["id"]["testResult"]["label"] for e in events if "testResult" in e["id"] and e.get("testResult", {}).get("status") in ("PASSED","FAILED") and not e["testResult"].get("cachedLocally") and not e["testResult"].get("executionInfo",{}).get("cachedRemotely")}
    prefix = "TestTCPSynSentUnreachable6/"
    network = "--- PASS: "+prefix+"net_unreachable" in text
    address = "--- PASS: "+prefix+"address_unreachable" in text
    failure = "--- FAIL: "+prefix+"address_unreachable" in text
    timeout = "Poll returned 0 ready file descriptors, expected 1" in text
    evidence.update(fixture=(path / "fixture").read_text().split()[0], summaries=summaries, network_pass=network, address_pass=address, address_failure=failure, timed_out=timeout, executed=sorted(present))
    if path.name == "stock":
        ok = rc != 0 and summaries == {label:"FAILED"} and present == {label} and network and not address and failure and timeout
    else:
        expected = {label}
        if path.name == "fixed":
            expected.add("//pkg/tcpip/network/ipv6:ipv6_nogo")
        ok = rc == 0 and summaries == {k:"PASSED" for k in expected} and expected <= present and network and address and not failure and not timeout
    evidence.update(exit=rc, verified=ok and len(invocations)==1, invocations=invocations)
    evidence["diagnostics"] = [line[:320] for line in text.splitlines() if ("Poll returned " in line or "got connect()" in line or line.startswith(("ERROR:", "--- FAIL:", "FAIL:"))) and not any(x in line.lower() for x in ("credential", "token", "remote_header", "bes_header"))][:8]
    return evidence


if __name__ == "__main__":
    root = Path(sys.argv[1])
    rows = {stage: arm(root / stage) for stage in ("native", "stock", "fixed")}
    product = all(rows[s]["verified"] for s in ("native", "stock", "fixed")) and len({rows[s].get("fixture") for s in ("native", "stock", "fixed")}) == 1
    ok = product
    report = {"verified":ok, "packets_verified":product, "candidate":os.environ["CANDIDATE"], "arms":rows}
    (root / "verdict.json").write_text(json.dumps(report,indent=2)+"\n")
    if len(sys.argv)==2:
        raise SystemExit(not ok)
