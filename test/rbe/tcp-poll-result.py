"""Read actual named GTest assertions and preserve independent lane outcomes."""
from collections import Counter
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as XML

root = Path(sys.argv[1])
expected = {f"AllInetTests/TcpSocketTest.HalfCloseReadiness{side}/{i}" for side in ("Local", "Peer") for i in range(2)}
result = {}
for name in ("poll-native","poll-stock"):
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
    verified = set(assertions) == expected and status == 0 and all(a["status"] == "OK" for a in assertions.values())
    import datetime, os, re
    source=(directory/"source").read_text().strip()
    verified &= source == os.environ["STOCK"]
    log=(directory/"bazel.log").read_text(errors="replace") if (directory/"bazel.log").is_file() else ""
    diagnostics=[s[:360] for line in log.splitlines() if (s:=line.strip()).startswith(("error:", "ERROR:", "Error:")) and not any(k in s.lower() for k in ("header", "key=", "token"))][:8]
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    census={test:a["details"] for test,a in assertions.items()}
    if name == "poll-stock":
        # Record each observed ABI difference. A compile error, missing case,
        # or timeout in the enclosing test target is not a socket observation.
        failures = [message for case in assertions.values() for message in case["details"]]
        verified = (source == os.environ["STOCK"] and set(assertions) == expected
                    and all(case["status"] in ("OK", "FAIL") for case in assertions.values())
                    and status == (3 if failures else 0)
                    and all(any(part in message for part in ("pfd.revents", "RetryEINTR(poll)", "RetryEINTR(epoll_wait)", "event.events"))
                            for message in failures))
    result[name]=dict(source=source,expected=sorted(expected),executed=sorted(assertions),outcomes=assertions,missing=sorted(expected-assertions.keys()),exit=status,verified=verified,completed=(directory/"end").read_text().strip(),verified_at=now,diagnostics=diagnostics,fixture=(directory/"fixture-sha256").read_text().split()[0],invocations=list(dict.fromkeys(re.findall(r"https://app.buildbuddy.io/invocation/([a-f0-9-]{36})",log))))
result["verified"]=all(a["verified"] for a in result.values()) and len({a["fixture"] for a in result.values()})==1
(root/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
sys.exit(not result["verified"])
