"""Read actual named GTest assertions and preserve independent lane outcomes."""
from collections import Counter
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as XML

root = Path(sys.argv[1])
tests = ("FINSentOnShutdownWithUnreadData", "RSTSentOnCloseWithUnreadData", "RSTSentOnCloseWithUnreadDataAllowsReadBuffered")
expected = {f"AllTCPSockets/TCPSocketPairTest.{test}/{i}" for test in tests for i in range(12)}
result = {}
for name in ("poll-native", "poll-stock", "poll-fixed"):
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
    if name == "poll-stock":
        failed = {test for test, case in assertions.items() if case["status"] == "FAIL"}
        verified = (set(assertions) == expected and status == 3
                    and failed == {test for test in expected if ".FINSentOnShutdown" in test}
                    and all(case["status"] in ("OK", "FAIL") for case in assertions.values())
                    and all(any("reset by peer" in msg.lower() or "ECONNRESET" in msg
                                for msg in assertions[test]["details"]) for test in failed))
    verified &= (source == os.environ["PRODUCT" if name == "poll-fixed" else "STOCK"] and (directory/"fixture-sha256").read_text().split()[0] == os.environ["FIXTURE_SHA256"])
    log=(directory/"bazel.log").read_text(errors="replace") if (directory/"bazel.log").is_file() else ""
    diagnostics=[s[:360] for line in log.splitlines() if (s:=line.strip()).startswith(("error:", "ERROR:", "Error:")) and not any(k in s.lower() for k in ("header", "key=", "token"))][:8]
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    census={test:a["details"] for test,a in assertions.items()}
    result[name]=dict(source=source,expected=sorted(expected),executed=sorted(assertions),outcomes=assertions,missing=sorted(expected-assertions.keys()),exit=status,verified=verified,completed=(directory/"end").read_text().strip(),verified_at=now,diagnostics=diagnostics,fixture=(directory/"fixture-sha256").read_text().split()[0],invocations=list(dict.fromkeys(re.findall(r"https://app.buildbuddy.io/invocation/([a-f0-9-]{36})",log))))
result["verified"]=all(a["verified"] for a in result.values()) and len({a["fixture"] for a in result.values()})==1
(root/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
sys.exit(not result["verified"])
