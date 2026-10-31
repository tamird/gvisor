import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

out = Path(sys.argv[1])
case_name = "IPv6UDPUnboundSockets/IPv6UDPUnboundExternalNetworkingSocketTest.TestIPv6MulticastIf/"
expected = {case_name + index for index in ("0", "1")}
arms = {}
for lane in ("control", "native", "fixed"):
    d = out / lane
    control = lane == "control"
    try:
        def read(name):
            return (d / name).read_text().strip()
        rows = {}
        log = read("socket_ipv6_udp_unbound_external_networking_test/test.log")
        for elem in ET.parse(d/"socket_ipv6_udp_unbound_external_networking_test"/"test.xml").iter("testcase"):
            name = elem.get("classname", "") + "." + elem.get("name", "")
            assert name not in rows, name
            errors = [x.get("message") or x.text or "" for x in elem if x.tag in ("error","failure")]
            run = elem.get("status") == "run" and elem.get("result") not in ("skipped","suppressed") and elem.find("skipped") is None
            witness = any("device only" in x and "setsockopt" in x and ("return: 0" in x or "Actual: 0" in x or "succeeded" in x or "successfully" in x) and ("EINVAL" in x or "Invalid argument" in x or "errno 22" in x or "errno: 22" in x) for x in errors)
            rows[name] = {"ran":run, "errors":errors, "expected_result":run and (witness if control else not errors)}
        nics = sorted(set(re.findall(r'name=(?:lo|eth0) addr=[^\r\n]+? index=\d+',log)))
        arms[lane] = {
            "rows":rows, "nics":nics, "source":read("source"), "exit":read("exit"),
            "fixture":read("fixture"), "product":read("product"),
            "start":read("start"), "end":read("end"),
            "ok": read("exit") == ("3" if control else "0")
                  and read("source") == os.environ["STOCK" if control else "CURRENT"]
                  and set(rows) == expected and all(v["expected_result"] for v in rows.values())
                  and any("name=lo " in i for i in nics) and any("name=eth0 " in i for i in nics),
        }
    except (AssertionError, OSError, ET.ParseError) as err:
        arms[lane] = {"ok":False, "missing_or_invalid": str(err)}
ok = all(l["ok"] for l in arms.values()) and len({l["fixture"] for l in arms.values()}) == 1
result = {"verified":ok, "arms":arms, "native_kernel":(out/"native-kernel").read_text().strip()}
(out/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps(result))
sys.exit(not ok)
