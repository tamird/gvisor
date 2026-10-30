"""Read actual named GTest assertions and preserve independent lane outcomes."""
from collections import Counter
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as XML

root = Path(sys.argv[1])
mapped = "SocketInetLoopback.MappedConnectReleasesBothWildcardBinds"
prefix = "AllTCPSockets/TCPSocketPairTest."
groups = ("TCPCork", "TCPCorkFlushOnClose", "TCPCorkFlushOnShutdown")
expected = {f"{prefix}{group}/{i}" for group in groups for i in range(12)}
result = {}
for name in sys.argv[2:] or ("mapped-native","mapped-guest","cork-native","cork-stock","cork-fixed"):
    directory = root/name
    status = int((directory/"exit").read_text()) if (directory/"exit").is_file() else None
    assertions = {}
    if (directory/"test.xml").is_file():
        for case in XML.parse(directory/"test.xml").iter("testcase"):
            test = case.get("classname","")+"."+case.get("name","")
            failures = case.findall("failure")+case.findall("error")
            assertions[test] = {
                "status": "SKIP" if case.find("skipped") is not None or case.get("status")=="notrun"
                          else "FAIL" if failures else "OK",
                "details": [node.text or node.get("message","") for node in failures],
            }
    if name.startswith("mapped"):
        exact = set(assertions)=={mapped}
        case = assertions.get(mapped, {})
        log = "\n".join(case.get("details",[]))
        verified = exact and ((name=="mapped-native" and status==0 and case["status"]=="OK") or
                              (name=="mapped-guest" and status==3 and case["status"]=="FAIL"
                               and len(case["details"])==1 and "v6.get()" in log
                               and ("EADDRINUSE" in log or "Address already in use" in log)))
    else:
        exact = set(assertions)==expected
        failures = {test for test,item in assertions.items() if item["status"]=="FAIL"}
        if name=="cork-stock":
            verified = (exact and status==3 and not any(item["status"]=="SKIP" for item in assertions.values())
                        and all("FIN never flushed before the older cork" in "\n".join(assertions[test]["details"])
                                for test in failures)
                        and all(any(f".{group}/" in test for test in failures) for group in groups[1:]))
        else:
            verified = exact and status==0 and all(item["status"]=="OK" for item in assertions.values())
    result[name] = {"exit":status,"verified":verified,"assertions":assertions,
                    "counts":dict(Counter(item["status"] for item in assertions.values()))}
    print(name,"exit",status,"verified",verified,result[name]["counts"])
(root/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
if not all(record["verified"] for record in result.values()):
    raise SystemExit(1)
