"""Verify the published delayed-ACK test suite from its own Bazel results."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree

from qualification_report import xml_cases

SOURCE = "10555b8fe8ee142f6a956ab3f7fbc717d38b0639"
OWNER = "//pkg/tcpip/transport/tcp/test/e2e:tcp_delayed_ack_test"
EXPECTED = {"TestDelayedACK", "TestDelayedACKTwoFullSegments", "TestDelayedACKFilledGap",
            "TestDelayedACKPiggyback", "TestDelayedACKLinkWriteError", "TestDelayedACKQuickAck/Set",
            "TestDelayedACKQuickAck/Clear", "TestDelayedACKRestore"}
work = Path(os.environ["RUNNER_TEMP"]) / "delayed-ack-review"
row = dict(source=SOURCE, owner=OWNER, bazel_exit=int(sys.argv[1]), verified=False)
try:
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    results = []
    with (work / "run.bep").open() as stream:
        for line in stream:
            ev = json.loads(line)
            if ev.get("id", {}).get("testResult", {}).get("label") == OWNER and "testResult" in ev:
                results.append(ev["testResult"])
    res, = results
    info = res.get("executionInfo", {})
    row.update(status=res["status"], strategy=info.get("strategy"),
               cached=bool(res.get("cachedLocally") or info.get("cachedRemotely")))
    outputs = Path("bazel-testlogs/pkg/tcpip/transport/tcp/test/e2e/tcp_delayed_ack_test")
    xml = outputs / "test.xml"
    row["case_counts"] = xml_cases(xml)
    for name in ("test.xml", "test.log"):
        row[name] = hashlib.sha256((outputs / name).read_bytes()).hexdigest()
    row["cases"] = {node.get("name"): (next((c.tag for c in node if c.tag in ("failure", "error", "skipped")), "passed"))
                    for node in ElementTree.parse(xml).iter("testcase")}
    assert row["bazel_exit"] == 0 and row["status"] == "PASSED"
    assert row["strategy"] == "linux-sandbox" and not row["cached"]
    assert EXPECTED <= row["cases"].keys() and all(s == "passed" for s in row["cases"].values())
    row["verified"] = True
finally:
    if not row["verified"]:
        log = (work / "run.console").read_text(errors="replace")
        row["diagnostics"] = [l[:500] for l in log.splitlines() if l.startswith(("ERROR:", "FAIL:", "--- FAIL:"))][:10]
        path = Path("bazel-testlogs/pkg/tcpip/transport/tcp/test/e2e/tcp_delayed_ack_test/test.log")
        if path.exists():
            row["packet_errors"] = [l.strip()[:350] for l in path.read_text(errors="replace").splitlines()
                                    if "tcp_delayed_ack_test.go:" in l or "--- FAIL:" in l][:15]
    (work / "receipt.json").write_text(json.dumps(row, indent=2) + "\n")
