"""Read the original XML and log of each exact packet-socket test action."""
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

root = Path(sys.argv[1])
rows = {}
case = "PacketSocketShutdown.RejectedShutdownLeavesReceiveOpen"
for arm in ("packet-native", "packet-hostinet"):
    path = root / arm
    if not (path / "exit").exists():
        continue
    def read(name):
        return (path / name).read_text().strip()
    body = (path / "test.log").read_text()
    target = "//test/syscalls:packet_socket_test_" + ("native" if arm == "packet-native" else "runsc_systrap_hostnet")
    observations = [[int(n) for n in row] for row in re.findall(r"PACKETRECV type=(\d+) shut=(\d+) result=(-?\d+) errno=(\d+) us=(\d+)", body)]
    cases = list(ElementTree.parse(path / "test.xml").iter("testcase"))
    native = arm == "packet-native"
    status = "PASSED" if native else "FAILED"
    events = [json.loads(line) for line in (path / "bep").open()]
    summary = [e["testSummary"]["overallStatus"] for e in events if e["id"].get("testSummary", {}).get("label") == target]
    attempts = [e["testResult"] for e in events if e["id"].get("testResult", {}).get("label") == target]
    original = {}
    if len(attempts) == 1:
        for file in ("test.log", "test.xml"):
            payload = (path / file).read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            outputs = [f for f in attempts[0]["testActionOutput"] if f["name"] == file]
            original[file] = bool(len(outputs) == 1 and (outputs[0].get("digest") == digest or outputs[0].get("uri", "").endswith(f"/blobs/{digest}/{len(payload)}")))
    ok = (read("source") == os.environ["PRODUCT"] and read("fixture-sha256").split()[0] == os.environ["FIXTURE_SHA256"]
          and read("target") == target and read("exit") == ("0" if native else "3") and summary == [status]
          and [a["status"] for a in attempts] == [status] and len(original) == 2 and all(original.values())
          and len(cases) == 1 and cases[0].get("classname") + "." + cases[0].get("name") == case
          and cases[0].get("status") != "notrun" and not any(c.tag in ("skipped", "error") for c in cases[0])
          and (cases[0].find("failure") is None) == native
          and len(observations) == 4 and {(t, s) for t, s, *_ in observations} == {(t, s) for t in (2, 3) for s in (0, 1)}
          and all((n, err) == (-1, 11) and us >= 100000 if native or s == 0 else (n, err) == (0, 0)
                  for _, s, n, err, us in observations))
    rows[arm] = dict(verified=ok, target=target, source=read("source"), fixture=read("fixture-sha256").split()[0],
                     start=read("start"), end=read("end"), exit=read("exit"), original=original,
                     summary=summary, observations=observations,
                     invocation=[e["started"]["uuid"] for e in events if "started" in e])
complete = len(rows) == 2 and all(r["verified"] for r in rows.values())
(root / "verdict.json").write_text(json.dumps(dict(verified=complete, evidence=rows), indent=2))
if len(sys.argv) == 2 and not complete:
    raise SystemExit(1)
