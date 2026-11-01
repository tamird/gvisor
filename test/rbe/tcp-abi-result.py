"""Read original lifecycle case results and the observed errors separately."""
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as XML

root=Path(sys.argv[1])
results={}
for lane in ("duplex-fixed",):
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
        w["cases"]={c.get("classname")+"."+c.get("name"):dict(skipped=c.get("status")=="notrun" or c.find("skipped") is not None,failures=[f.get("message","")[:650] for f in c.findall("failure")+c.findall("error")]) for c in cases}
        s=(directory/"test.log").read_text(errors="replace")
        w["observations"]=[list(map(int,m)) for m in re.findall(r"BIND mode=(\d+) before=(\d+),(\d+) refused=(\d+),(\d+) contender=(-?\d+) retry=(-?\d+),(\d+) local=(\d+),(\d+) keepalive=(\d+) delivered=(\d+)",s)]
        assert len(w["observations"])==5 and {m[0] for m in w["observations"]}==set(range(5))
        w["operations"]=re.findall(r"LIFE arm=(\d+) phase=(\w+) op=(-?\d+) errno=(\d+) q=(-?\d+) qerrno=(\d+)",s)
        w["errors"]=re.findall(r"LIFE arm=(\d+) error_read=(\d+) value=(\d+)",s)
        w["delivered"]=re.findall(r"LIFE arm=(\d+) delivered=(\d+)",s)
        assert set(w["cases"])=={"TcpConnectTest.InspectBindAcrossRefusal","TcpOutputQueueTest.ObserveDisconnectAndReuse"} and all(not row["skipped"] and not row["failures"] for row in w["cases"].values()) and not w["exit"]
        by_mode={r[0]:r for r in w["observations"]}
        for mode,row in by_mode.items():
            assert row[5]==[-1,0,98,98,0][mode] and row[6:8]==[0,0] and row[-2:]==[1,1], row
            if mode in (2,3): assert row[2]==row[9]
            assert row[3]==row[1]
        ops={(int(arm),phase):(int(rc),int(error)) for arm,phase,rc,error,*_ in w["operations"]}
        assert ops[0,"reconnect1"]==(0,0) and ops[1,"reconnect1"]==(-1,103) and ops[3,"reconnect1"]==(-1,111)
        assert all(ops[n,"reconnect2"]==(-1,115) for n in [1,3])
        assert w["errors"]==[("0","0","0"),("0","1","0"),("1","0","111"),("1","1","0")]
        assert w["delivered"]==[(str(n),"1") for n in range(4)]
        w["verified"]=True
    except Exception as err:
        w["error"]=f"{type(err).__name__}: {str(err)[:400]}"
success=all(x["verified"] for x in results.values()) and len({w["fixture"] for w in results.values()})==1
(root/"verdict.json").write_text(json.dumps(dict(verified=success,arms=results),indent=2)+"\n")
sys.exit(not success)
