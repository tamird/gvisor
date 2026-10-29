"""Read one existing Bazel test result; allow only the measured RACK predicate failures."""
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlparse
import sys
import xml.etree.ElementTree as ET

out, name, source, owner, status = sys.argv[1:]
out=Path(out)
r=dict(source=source, owner=owner, arm=name, command_exit=int(status), verdict="INCOMPLETE")
try:
    bep=[json.loads(s) for s in (out/"events.jsonl").read_text().splitlines()]
    r["invocation"]=next(e["started"]["uuid"] for e in bep if "started" in e)
    finished,=[e["finished"]["exitCode"] for e in bep if "finished" in e]
    r["bazel_exit"]=int(finished.get("code",0))
    r["bazel_exit_name"]=finished.get("name","")
    test,=[e["testResult"] for e in bep if e.get("id",{}).get("testResult",{}).get("label")==owner]
    r["test_status"]=test["status"]
    r["remote"]=test.get("executionInfo",{}).get("strategy")=="remote"
    r["cached"]=bool(test.get("cachedLocally") or test.get("executionInfo",{}).get("cachedRemotely"))
    assert r["remote"] and not r["cached"]
    r["reports"]={}
    by_name={e["name"]:e for e in test["testActionOutput"]}
    data={}
    for f in ("test.xml","test.log"):
        b=(Path("bazel-testlogs")/owner[2:].replace(":","/")/f).read_bytes()
        h=sha256(b).hexdigest()
        entry=by_name[f]
        if "digest" in entry:
            assert h==entry["digest"] and len(b)==int(entry.get("length",len(b)))
        else:
            uri=urlparse(entry["uri"])
            actual=Path("bazel-testlogs")/owner[2:].replace(":","/")/f
            if uri.scheme=="file":
                assert Path(unquote(uri.path)).resolve()==actual.resolve()
            else:
                assert uri.scheme=="bytestream" and uri.path.endswith("/"+h+"/"+str(len(b)))
        r["reports"][f]={"sha256":h,"bytes":len(b)}
        data[f]=b
    rows=ET.fromstring(data["test.xml"]).findall(".//testcase")
    cases={e.attrib["name"]:next((c.tag for c in e if c.tag in {"skipped","failure","error"}),"pass") for e in rows}
    assert len(rows)==len(cases)==3
    r["cases"]=cases
    if name == "linux":
        top = "TestLinuxMappedConnectReleasesBothBoundFamilies"
        assert cases == {top: "pass", top + "/IPv4": "pass", top + "/IPv6Only": "pass"}
        assert r["test_status"] == "PASSED" and r["bazel_exit"] == int(status) == 0
        modes = re.findall(r"MAPPED rebind=(IPv4|IPv6Only) same_port=true peer_reset=true", data["test.log"].decode())
        assert modes == ["IPv4","IPv6Only"]
        r["rebound_after_peer_reset"] = modes
    else:
        assert name=="rack"
        a,b,c="TestRACKRepairsLostBatch","TestRACKCountsTailProbeBeforeACK","TestRACKCountsTailProbe"
        assert set(cases)=={a,b,c} and set(cases.values())<={"pass","failure"}
        text=data["test.log"].decode(errors="replace")
        repaired=[list(map(int,x)) for x in re.findall(r"FLIGHT repaired bytes \[(\d+),(\d+)\)",text)]
        r["repairs"]=repaired
        assert repaired and repaired==[[i*496,(i+1)*496] for i in range(len(repaired))] and len(repaired)<=3
        assert (cases[a]=="pass")== (len(repaired)==3)
        if cases[a]=="failure":
            entry=next(e for e in rows if e.attrib["name"]==a)
            assert re.search("timed out|timeout|waiting for.*packet|no packet|Packet wasn.t written out",text,re.I)
        pre,=re.findall(r"FLIGHT before ACK: P1 P2 and two P3 copies, cwnd=(\d+) new packets=(\d+) want=(\d+)",text)
        w,n,lim=map(int,pre)
        assert w-lim==4 and n in (lim,lim+1) and (cases[b]=="pass")==(n==lim)
        r["before_ack"]={"cwnd":w,"new_packets":n,"available_slots":lim}
        after,=re.findall(r"FLIGHT observed P3 twice; SACKed only P2; repaired P1. Outstanding=(\d+) cwnd=(\d+)",text)
        second,=re.findall(r"FLIGHT new packets=(\d+); max with both P3 copies=(\d+)",text)
        o,w=map(int,after);n,lim=map(int,second)
        assert lim==w-3 and n in (lim-1,lim,lim+1) and o in (2,3,4) and (cases[c]=="pass")== (n==lim and o==3)
        r["after_sack"]={"outstanding":o,"cwnd":w,"new_packets":n,"available_slots":lim}
        passed=set(cases.values())=={"pass"}
        assert (r["test_status"]=="PASSED")==passed and (r["bazel_exit"]==0)==passed and (int(status)==0)==passed
    r["verdict"]="COMPLETE"
except Exception as exc:
    r["exception"]=type(exc).__name__
    r["line"]=exc.__traceback__.tb_lineno
finally:
    (out/"result.json").write_text(json.dumps(r,sort_keys=True,indent=2)+"\n")
sys.exit(int(r["verdict"]!="COMPLETE"))
