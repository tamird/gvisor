"""Keep actual test records, including the distinguishable stock error."""
from pathlib import Path
import json, os, re, sys
import xml.etree.ElementTree as ET
root=Path(sys.argv[1])
arms={}
for name,source in (("duplex-native","STOCK"),("duplex-stock","STOCK"),("duplex-fixed","FIXED")):
    q=root/name
    w={"pass":False};arms[name]=w
    try:
        w["source"]=(q/"source").read_text().strip()
        assert w["source"]==os.environ[source]
        w["fixture"]=(q/"fixture-sha256").read_text().split()[0]
        w["exit"]=int((q/"exit").read_text())
        log=(q/"bazel.log").read_text(errors="replace")
        w["invocations"]=list(dict.fromkeys(re.findall(r"https://app.buildbuddy.io/invocation/[a-f0-9-]+",log)))
        w["bazel_errors"]=[s[:320] for s in log.splitlines() if s.startswith("ERROR:") and not any(t in s.lower() for t in ("token","key","header"))][:8]
        cases=list(ET.parse(q/"test.xml").iter("testcase"))
        w["tests"]={c.get("classname")+"."+c.get("name"):{"skipped":c.get("status")=="notrun" or c.find("skipped") is not None,"failures":[e.get("message","")[:1500] for e in c.findall("failure")+c.findall("error")]} for c in cases}
        assert set(w["tests"])=={"TcpConnectTest.RefusalIsReportedOnce"}
        record=next(iter(w["tests"].values()))
        assert not record["skipped"]
        if name=="duplex-stock":
            assert w["exit"]==3 and len(record["failures"])==2 and all("Which is: 111" in m for m in record["failures"])
        else:
            assert w["exit"]==0 and not record["failures"]
        w["pass"]=True
    except Exception as e:
        w["error"]=f"{type(e).__name__}: {e}"
ok=all(q["pass"] for q in arms.values()) and len({q.get("fixture") for q in arms.values()})==1
(root/"verdict.json").write_text(json.dumps({"verified":ok,"arms":arms},indent=2)+"\n")
sys.exit(not ok)
