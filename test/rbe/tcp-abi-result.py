"""Read actual named GTest assertions and preserve independent lane outcomes."""
from collections import Counter
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as XML

root = Path(sys.argv[1])
expected={f"AllInetTests/TcpSocketTest.{n}/{i}" for n in ("NoLingerOnProcessExit","AbortiveLingerOnExit") for i in (1,)}
result = {}
for name in ("duplex-stock","duplex-fixed"):
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
    if name == "duplex-stock":
        verified=(set(assertions)==expected and status==3 and all(
            (r["status"]=="FAIL" and any("start" in x and "Seconds" in x for x in r["details"]))
            if ".NoLingerOnProcessExit/" in ident else r["status"]=="OK" for ident,r in assertions.items()))
    import datetime, os, re
    source=(directory/"source").read_text().strip() if (directory/"source").is_file() else ""
    verified &= source == os.environ["FIXED" if name=="duplex-fixed" else "STOCK"]
    log=(directory/"bazel.log").read_text(errors="replace") if (directory/"bazel.log").is_file() else ""
    diagnostics=[s[:360] for line in log.splitlines() if (s:=line.strip()).startswith(("error:", "ERROR:", "Error:")) and not any(k in s.lower() for k in ("header", "key=", "token"))][:8]
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    result[name]=dict(source=source,expected=sorted(expected),executed=sorted(assertions),outcomes=assertions,missing=sorted(expected-assertions.keys()),exit=status,verified=verified,completed=(directory/"end").read_text().strip() if (directory/"end").is_file() else None,verified_at=now,diagnostics=diagnostics,fixture=(directory/"fixture-sha256").read_text().split()[0] if (directory/"fixture-sha256").is_file() else None,invocations=list(dict.fromkeys(re.findall(r"https://app.buildbuddy.io/invocation/([a-f0-9-]{36})",log))))
result["verified"]=all(a["verified"] for a in result.values()) and len({a["fixture"] for a in result.values()})==1
(root/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
sys.exit(not result["verified"])
