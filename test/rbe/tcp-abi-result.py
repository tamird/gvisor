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
        w["observations"]=[list(map(int,m)) for m in re.findall(r"BIND mode=(\d+) before=(\d+),(\d+) refused=(\d+),(\d+) contender=(-?\d+) retry=(-?\d+),(\d+) local=(\d+),(\d+) keepalive=(\d+) delivered=(\d+)",s)]
        assert len(w["observations"])==5 and {m[0] for m in w["observations"]}==set(range(5))
        assert w["case"]=="TcpConnectTest.InspectBindAcrossRefusal" and case.get("status")!="notrun" and case.find("skipped") is None and not w["failures"] and w["exit"]==0
        if lane=="duplex-native": assert all(m[-1]==1 for m in w["observations"])
        w["verified"]=True
    except Exception as err:
        w["error"]=f"{type(err).__name__}: {str(err)[:400]}"
success=all(x["verified"] for x in results.values()) and len({w["fixture"] for w in results.values()})==1
(root/"verdict.json").write_text(json.dumps(dict(verified=success,arms=results),indent=2)+"\n")
sys.exit(not success)
