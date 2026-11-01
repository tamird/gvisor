"""Inspect the three original guest/native reports and print the lifecycle observed."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

root = Path(sys.argv[1])
receipts = {}
pair = "AllTCPSockets/TCPSocketPairTest."
expected = {pair + case + "/" + str(i) for case in ("OutputQueueSurvivesReset", "OutputQueueThroughFinAndReset") for i in range(12)}
lifecycle = "TcpOutputQueueTest.ObserveDisconnectAndReuse"
expected.add(lifecycle)
for name in ("duplex-native", "duplex-stock", "duplex-fixed"):
    directory = root / name
    src = (directory / "source").read_text().strip()
    w = { "source": src, "status": "INCOMPLETE" }
    receipts[name] = w
    try:
        assert src == os.environ["FIXED" if name == "duplex-fixed" else "STOCK"]
        w["fixture_sha256"] = (directory / "fixture-sha256").read_text().split()[0]
        w["exit"] = int((directory / "exit").read_text())
        bazel = (directory / "bazel.log").read_text(errors="replace")
        w["invocations"] = list(dict.fromkeys(re.findall(r"https://app.buildbuddy.io/invocation/([a-f0-9-]+)", bazel)))
        w["errors"] = [ln[:200] for ln in bazel.splitlines() if ln.startswith(("ERROR:", "error:")) and not any(s in ln.lower() for s in ("token","key=","header"))][:6]
        cases = {}
        for c in ET.parse(directory / "test.xml").iter("testcase"):
            case = c.get("classname") + "." + c.get("name")
            failures = c.findall("failure") + c.findall("error")
            cases[case] = {"failures": [f.get("message","")[:400] for f in failures], "skip": c.get("status") == "notrun" or c.find("skipped") is not None}
        w["cases"] = cases
        assert set(cases) == expected, sorted(set(cases)^expected)
        assert not any(c["skip"] for c in cases.values())
        assert not cases[lifecycle]["failures"], cases[lifecycle]
        p = (directory / "test.log").read_text(errors="replace")
        w["operations"] = [list(m) for m in re.findall(r"LIFE arm=(\d+) phase=(\w+) op=(-?\d+) errno=(\d+) q=(-?\d+) qerrno=(\d+)",p)]
        w["delivered"] = dict(re.findall(r"LIFE arm=(\d+) delivered=(\d+)",p))
        w["established"] = [list(map(int,m)) for m in re.findall(r"OUTQ path=(\d+) sent=(\d+) unread_before=(\d+) after_fin=(\d+) final=(-?\d+)",p)]
        phases = {(int(a),b) for a,b,*_ in w["operations"]}
        assert phases == ({(a,b) for a in (0,1) for b in ("refused","cleared","reconnect1")} | {(2,b) for b in ("disconnect1","disconnect2","reconnect1")} | {(1,"reconnect2")}), phases
        if name == "duplex-stock":
            assert w["exit"] == 3 and all(c["failures"] and "Protocol not available" in c["failures"][0] for k,c in cases.items() if k!=lifecycle)
        else:
            assert w["exit"] == 0 and not any(c["failures"] for c in cases.values())
            vals = w["established"]
            assert len(vals)==36 and {r[0] for r in vals}=={0,1,2}
            assert all(u>0 and q==s-u+1 and end==(q if i==2 else 0) for i,s,u,q,end in vals)
        w["status"] = "OBSERVED"
    except Exception as err:
        w["reason"] = f"{type(err).__name__}: {str(err)[:500]}"
verified = all(r["status"]=="OBSERVED" for r in receipts.values()) and len({r["fixture_sha256"] for r in receipts.values()})==1
payload = dict(verified=verified,completed=datetime.now(timezone.utc).isoformat(),arms=receipts)
(root / "verdict.json").write_text(json.dumps(payload,indent=2)+"\n")
print(json.dumps({k:(r["status"],r.get("reason"),r.get("delivered")) for k,r in receipts.items()}))
sys.exit(not verified)
