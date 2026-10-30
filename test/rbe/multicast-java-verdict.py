import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
root = Path(sys.argv[1])
verdict = {}
for name in ("wire-native", "wire-stock", "wire-current", "options-current"):
    d = root / name
    cases = {}
    for tc in ET.parse(d / "test.xml").iter("testcase") if (d / "test.xml").exists() else ():
        cases[tc.get("name")] = ("SKIP" if tc.find("skipped") is not None or tc.get("status") == "notrun"
                                 else "FAIL" if tc.find("failure") is not None or tc.find("error") is not None
                                 else "PASS")
    expected = ({"IPv4SmallOptionBuffers", "IndependentIPv4AndIPv6Values", "IPv6ValueLimits"} if name == "options-current"
                else {"ConnectedMappedMulticast/0", "ConnectedMappedMulticast/1"})
    rc = (d / "exit").read_text().strip() if (d/"exit").is_file() else None
    stock = name == "wire-stock"
    ok = set(cases) == expected and all(v == ("FAIL" if stock else "PASS") for v in cases.values()) and rc == ("1" if stock else "0")
    verdict[name] = {"cases":cases, "exit":rc, "verified":ok}
d=root/"jdk-current"
log=(d/"jtreg.log").read_text() if (d/"jtreg.log").is_file() else ""
rc=(d/"exit").read_text().strip() if (d/"exit").is_file() else None
verdict["jdk-current"]={"exit":rc,"verified":rc=="0" and "Test results: passed: 1" in log and "Test family=INET6" in log and "-Djava.net.preferIPv4Stack=true" in log}
d=root/"owners"
tests=json.loads((d/"results.json").read_text()) if (d/"results.json").exists() else {}
targets=set((d/"targets").read_text().splitlines()) if (d/"targets").exists() else set()
rc=(d/"exit").read_text().strip() if (d/"exit").is_file() else None
verdict["owners"]={"tests":tests,"exit":rc,"verified":rc=="0" and set(tests)==targets and len(tests)==8 and all(v in ("PASSED","FLAKY") for v in tests.values())}
(root/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
print(json.dumps(verdict,indent=2))
if not all(v["verified"] for v in verdict.values()): raise SystemExit(1)
