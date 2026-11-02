"""Verify the recovered refusal and poll controls from their Bazel and GTest results."""
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ROOT = Path(sys.argv[1])
POLL = "tcp_socket_test"
REFUSAL = "socket_ip_tcp_generic_loopback_test"
NOGO = "//pkg/tcpip/transport/tcp:tcp_nogo"
CASES = {
    POLL: {f"AllInetTests/SimpleTcpSocketTest.{case}/{family}" for family in (0, 1)
           for case in ("NonBlockingConnectRefused", "SynRcvdOnListenerShutdown")},
    REFUSAL: {"TcpConnectTest.RefusalIsReportedOnce"},
}


def inspect(arm):
    d = ROOT / arm
    if not (d / "exit").exists():
        return {"verified": False, "reason": "no completed command"}
    rc = int((d / "exit").read_text())
    source = (d / "source").read_text().strip()
    want = os.environ["STOCK" if arm == "stock" else "CANDIDATE"]
    analyzer = arm.endswith("-nogo")
    native = arm == "native"
    suffix = "native" if native else "runsc_ptrace"
    targets = {NOGO: None} if analyzer else {f"//test/syscalls:{name}_{suffix}": name for name in CASES}
    attempts, summaries, cases, diagnostics = {}, {}, {}, []
    finish = inv = None
    bep = d / "bep"
    for line in bep.open() if bep.exists() else ():
        e = json.loads(line)
        if "started" in e:
            inv = e["started"].get("uuid")
        if "finished" in e:
            finish = e["finished"].get("exitCode", {}).get("code", 0)
        if "testResult" in e:
            r = e["testResult"]; label = e["id"]["testResult"]["label"]
            attempts.setdefault(label, []).append({
                "status": r.get("status"), "runner": r.get("executionInfo", {}).get("strategy"),
                "cached": any((r.get("cachedLocally"), r.get("cachedRemotely"),
                               r.get("executionInfo", {}).get("cachedRemotely")))})
        if "testSummary" in e:
            summaries[e["id"]["testSummary"]["label"]] = e["testSummary"].get("overallStatus")
    ok = source == want and bool(inv) and finish == rc == (3 if arm == "stock" else 0)
    ok &= set(attempts) == set(summaries) == set(targets)
    for label, owner in targets.items():
        neg = arm == "stock" and owner == POLL
        status = "FAILED" if neg else "PASSED"
        results = attempts.get(label, [])
        ok &= len(results) == 1 and summaries.get(label) == status
        if results:
            ok &= results[0] == {"status": status, "runner": "local" if native else "remote", "cached": False}
        if analyzer:
            continue
        xml = d / (owner + ".xml")
        actual = {}
        if xml.exists():
            for c in ET.parse(xml).iter("testcase"):
                name = c.get("classname", "") + "." + c.get("name", "")
                fail = c.findall("failure") + c.findall("error")
                assert name not in actual, name
                actual[name] = ("SKIPPED" if c.find("skipped") is not None or c.get("status") == "notrun"
                                 else "FAIL" if fail else "PASS")
                if fail:
                    msgs = [f.get("message", f.text or "")[:600] for f in fail]
                    diagnostics.append([name, msgs])
                    if neg and ".NonBlockingConnectRefused/" in name:
                        ok &= any("poll_fd.revents" in x for x in msgs)
        cases[owner] = actual
        ok &= actual == {n: "FAIL" if neg and ".NonBlockingConnectRefused/" in n else "PASS"
                         for n in CASES[owner]}
    log = d / "bazel.log"
    build_errors = []
    if log.is_file():
        for line in log.open(errors="replace"):
            if line.startswith(("ERROR:", "FAIL:", "error:")) and not any(
                    k in line.lower() for k in ("credential", "token", "api-key", "x-buildbuddy", "remote_header", "bes_header")):
                build_errors.append(line.strip()[:500])
                if len(build_errors) == 6:
                    break
    return {"build_errors": build_errors, "source": source, "exit": rc, "finished": finish, "invocation": inv, "attempts": attempts,
            "summaries": summaries, "cases": cases, "diagnostics": diagnostics, "verified": bool(ok),
            "start": (d / "start").read_text().strip(), "end": (d / "end").read_text().strip()}


rows = {}
for arm in ("native", "stock", "fixed", "amd64-nogo", "arm64-nogo"):
    try:
        rows[arm] = inspect(arm)
    except Exception as exc:
        rows[arm] = {"verified": False, "reason": str(exc)[:250]}
ok = all(r["verified"] for r in rows.values())
out = {"rows": rows, "verified": ok, "verdict_at": datetime.now(timezone.utc).isoformat()}
(ROOT / "verdict.json").write_text(json.dumps(out, indent=2) + "\n")
print({name: r["verified"] for name, r in rows.items()})
if len(sys.argv) == 2:
    sys.exit(not ok)
