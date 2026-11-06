"""Check the original native TCP TestRunner outputs."""
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlsplit

work, phase, source, test_blob, rc = sys.argv[1:]
root = Path(work)
target = "//pkg/tcpip/transport/tcp:tcp_test"
cases = {
    "TestSegmentQueueBoundsZeroPayloadSegments",
    "TestSegmentQueueAllowsZeroPayloadWhenBufferFull",
    "TestSegmentQueueFrozen",
}
if phase == "correction":
    cases.add("TestSegmentQueueTwoProducersAtLimit")
row = {"source": source, "testBlob": test_blob, "exit": int(rc), "verified": False}
attempts = []
bep = root / (phase + ".bep")
for line in bep.open() if bep.is_file() else ():
    event = json.loads(line)
    result = event.get("testResult")
    if not result or event["id"].get("testResult", {}).get("label") != target:
        continue
    info = result.get("executionInfo", {})
    action = {"status": result["status"], "cached": bool(result.get("cachedLocally") or info.get("cachedRemotely"))}
    attempts.append(action)
    for item in result.get("testActionOutput", []):
        name = item.get("name"); uri = urlsplit(item.get("uri", ""))
        if name not in ("test.log", "test.xml") or uri.scheme != "file":
            continue
        data = Path(unquote(uri.path)).read_bytes()
        action[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        (root / (phase + "-" + name)).write_bytes(data)
        if name == "test.xml":
            records = list(ET.fromstring(data).iter("testcase"))
            action["xmlCases"] = [c.get("name", "") for c in records]
            action["nonpassing"] = sum(any(c.find(tag) is not None for tag in ("failure", "error", "skipped"))
                                        or c.get("status") == "notrun" for c in records)
        else:
            text = data.decode(errors="replace")
            action["goPasses"] = re.findall(r"(?m)^\s*--- PASS: (TestSegmentQueue\w+) ", text)
            metrics = re.search(r"admission limit=(\d+) charge=(\d+) accepted=(\d+) after=(\d+) drained=(\d+)", text)
            if metrics:
                action["boundary"] = dict(zip(("limit","charge","accepted","after","drained"), map(int, metrics.groups())))
row["attempts"] = attempts
if len(attempts) == 1:
    a = attempts[0]
    row["verified"] = (int(rc) == 0 and a["status"] == "PASSED" and not a["cached"]
                       and set(a.get("goPasses", [])) == cases and set(a.get("xmlCases", [])) == cases
                       and a.get("nonpassing") == 0 and len(a["xmlCases"]) == len(cases))
    if phase == "correction":
        m = a.get("boundary", {})
        row["verified"] = (row["verified"] and m.get("accepted") == 1 and m.get("charge", 0) > 0
                           and m["after"] == m["limit"] + m["charge"] and m["drained"] == m["limit"])
(root / (phase + ".json")).write_text(json.dumps(row, indent=2) + "\n")
