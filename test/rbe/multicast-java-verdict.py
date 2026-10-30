from pathlib import Path
import json
import sys
import xml.etree.ElementTree as ET

root = Path(sys.argv[1])
wanted = {"IPv4SmallOptionBuffers", "IndependentIPv4AndIPv6Values", "IPv6ValueLimits"}
out = {}
valid = True
for name in ("current-native", "stock-guest", "current-guest"):
    d = root / name
    cases = {}
    if (d / "test.xml").exists():
        for node in ET.parse(d / "test.xml").iter("testcase"):
            if node.get("classname") == "MulticastSocketOptions":
                failed = node.find("failure") is not None or node.find("error") is not None
                skipped = node.find("skipped") is not None or node.get("status") == "notrun"
                cases[node.get("name")] = "SKIP" if skipped else "FAIL" if failed else "PASS"
    rc=(d / "exit").read_text().strip() if (d / "exit").exists() else None
    stock=name == "stock-guest"
    passed=(set(cases) == wanted and all(v == ("FAIL" if stock else "PASS") for v in cases.values()) and rc==("3" if stock else "0"))
    out[name]={"cases":cases,"exit":rc,"verified":passed}
    valid &= passed
for name in ("native", "stock", "current"):
    d = root / ("jdk-" + name)
    rc=(d / "exit").read_text().strip() if (d / "exit").exists() else None
    log=(d/"jtreg.log").read_text() if (d/"jtreg.log").exists() else ""
    msgs=[x.strip() for x in log.splitlines() if any(y in x for y in ("Test family=", "Test results:", "Protocol not available", "testTimeToLive(", "TEST RESULT:"))]
    if name=="stock":
        passed=rc not in (None,"0") and "Protocol not available" in log and "DatagramSocketMulticasting.testTimeToLive" in log
    else:
        passed=rc=="0" and "Test results: passed: 1" in log and "Test family=INET" in log and "-Djava.net.preferIPv4Stack=true" in log
    valid &= passed
    out["jdk-"+name]={"exit":rc,"verified":passed,"summary":msgs[:16]}
(root/"verdict.json").write_text(json.dumps(out,indent=2)+"\n")
print(json.dumps(out,indent=2))
if not valid: raise SystemExit(1)
