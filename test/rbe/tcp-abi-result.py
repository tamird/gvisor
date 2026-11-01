"""Read original lifecycle case results and the observed errors separately."""
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as XML

root=Path(sys.argv[1])
results={}
for lane in ("duplex-native","duplex-fixed"):
    directory=root/lane
    w={"verified":False};results[lane]=w
    try:
        w["source"]=(directory/"source").read_text().strip()
        assert w["source"]==os.environ["FIXED"]
        w["fixture"]=(directory/"fixture-sha256").read_text().split()[0]
        w["exit"]=int((directory/"exit").read_text())
        log=(directory/"bazel.log").read_text(errors="replace")
        w["links"]=list(dict.fromkeys(re.findall(r"https://app.buildbuddy.io/invocation/([a-f0-9-]+)",log)))
        cases=list(XML.parse(directory/"test.xml").iter("testcase"))
        assert len(cases)==1
        case=cases[0]
        w["case"]=case.get("classname")+"."+case.get("name")
        w["failures"]=[str(f.get("message"))[:400] for f in case.findall("failure")+case.findall("error")]
        s=(directory/"test.log").read_text(errors="replace")
        w["operations"]=[list(m) for m in re.findall(r"LIFE arm=(\d+) phase=(\w+) op=(-?\d+) errno=(\d+) q=(-?\d+) qerrno=(\d+)",s)]
        w["errors"]=[list(map(int,m)) for m in re.findall(r"LIFE arm=(\d+) error_read=(\d+) value=(\d+)",s)]
        w["delivered"]=dict(re.findall(r"LIFE arm=(\d+) delivered=(\d+)",s))
        assert w["case"]=="TcpOutputQueueTest.ObserveDisconnectAndReuse" and case.get("status")!="notrun" and case.find("skipped") is None and not w["failures"] and w["exit"]==0
        assert set(w["delivered"])=={"0","1","2","3"} and len(w["errors"])==4
        phases={(int(a),b) for a,b,*_ in w["operations"]}
        assert {(a,b) for a in (0,1,3) for b in ("refused","cleared","reconnect1")} | {(2,b) for b in ("disconnect1","disconnect2","reconnect1")} <= phases
        if lane=="duplex-native":
            assert all(v=="1" for v in w["delivered"].values())
        w["verified"]=True
    except Exception as err:
        w["error"]=f"{type(err).__name__}: {str(err)[:400]}"
success=all(x["verified"] for x in results.values()) and len({w["fixture"] for w in results.values()})==1
(root/"verdict.json").write_text(json.dumps(dict(verified=success,arms=results),indent=2)+"\n")
sys.exit(not success)
