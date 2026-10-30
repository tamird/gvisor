import datetime
import json
import re
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

root = Path(sys.argv[1])
cases = ("two", "three", "threeSeparate")
result = {"target": "//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test", "expected": list(cases), "arms": {}}
ok = True
for label, head in zip(("old", "new"), sys.argv[2:]):
    sub = root / label
    arm = result["arms"][label] = {"expectedHead": head, "cases": {}, "missing": [], "unexpected": [], "skips": []}
    try:
        arm["head"] = (sub / "head").read_text().strip()
        arm["exit"] = int((sub / "exit").read_text())
        arm["testFinishedUTC"] = (sub / "finished").read_text().strip()
        log = (sub / "test.log").read_text()
        tree = ET.parse(sub / "test.xml")
        names = [x.attrib["name"] for x in tree.iter("testcase")]
        for name in cases:
            full = f"TestRACKWireCredit/{name}"
            arm["cases"][name] = rec = {}
            matches = re.findall(r"    --- (PASS|FAIL|SKIP): " + full + r" [(]", log)
            if names.count(full) != 1 or len(matches) != 1:
                arm["missing"].append(name)
                continue
            rec["status"] = matches[0]
            if rec["status"] == "SKIP":
                arm["skips"].append(name)
            part = log.split("=== RUN   " + full + "\n", 1)[1].split("=== RUN", 1)[0]
            row = re.search(r"credit=(-?\d+) rtt=(\S+) window=(\S+) reordered=(\w+) retransmit=(\w+)", part)
            if row:
                rec.update({"credit": int(row[1]), "rtt": row[2], "window": row[3], "reord": row[4], "wireRetransmission": row[5]})
            wanted_credit = {"two": 2, "three": 3, "threeSeparate": 3}[name] if label == "new" or name == "threeSeparate" else 1
            wants_wire = wanted_credit >= 3
            want_status = "FAIL" if label == "old" and name == "three" else "PASS"
            if not (row and rec["status"] == want_status and rec["credit"] == wanted_credit and rec["rtt"] == "100ms" and rec["window"] == ("0s" if wants_wire else "25ms") and rec["reord"] == "false" and rec["wireRetransmission"] == str(wants_wire).lower()):
                arm["unexpected"].append(name)
        ok = ok and arm["head"] == head and arm["exit"] == (3 if label == "old" else 0) and not arm["missing"] and not arm["skips"] and not arm["unexpected"]
    except (FileNotFoundError, ValueError, IndexError, ET.ParseError) as error:
        arm["error"] = str(error)
        ok = False
result["status"] = "verified" if ok else "unverified"
result["verdictUTC"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
(root / "verdict.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
raise SystemExit(0 if ok else 1)
