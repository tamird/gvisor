"""Retain only the named TCP test results, measured events and report identities."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

out, code = Path(sys.argv[1]), int(sys.argv[2])
owner = os.environ["QUALIFICATION_TARGETS"]
suite = os.environ["QUALIFICATION_TEST_FILTER"].strip("^$")
natural = suite == "TestVirtualTCPRACKFlight"
unit = owner == "//pkg/tcpip/transport/tcp:tcp_test"
expected = {"TestRACKPipeCopyAccounting"} if unit else ({suite} if natural else {"TestRACKPartialSoftwareGSOWrite", "TestRACKPRRFlight", "TestRACKPRRFlight/reno", "TestRACKPRRFlight/cubic"})
fixture = "84dcd85d055f54c29b1aa4002d244988217605f4"
product = "e211327319e6566d590e79eea1ab8fb0e3818d55"
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
result = {"status": "FAIL", "source": git("rev-parse", "HEAD"), "fixture": fixture, "product": product, "owner": owner, "native_exit": code, "modes": {}}
stage = "source"
try:
    assert result["source"] == os.environ["QUALIFICATION_COMMIT"] and git("rev-parse", fixture+"^") == "bd19388971ad63c0c94c11687b98f8c213d600c1"
    subprocess.run(["git","merge-base","--is-ancestor",fixture,product],check=True)
    subprocess.run(["git","merge-base","--is-ancestor",fixture,"HEAD"],check=True)
    subprocess.run(["git","diff","--exit-code",product,"HEAD","--","pkg"],check=True)
    rows = [json.loads(line) for line in (out / "events.jsonl").open()]
    stage = "BEP"
    attempts = [r for r in rows if r.get("id", {}).get("testResult", {}).get("label") == owner]
    result["attempts"] = len(attempts)
    result["failure_categories"] = sorted({reason if re.fullmatch(r"[A-Z_]{1,40}", reason) else "UNSPECIFIED" for r in rows if "aborted" in r for reason in [r["aborted"].get("reason", "UNSPECIFIED")]})
    assert len(attempts) == 1
    test = attempts[0]["testResult"]
    result["remote"] = test.get("executionInfo", {}).get("strategy") == "remote"
    result["cached"] = bool(test.get("cachedLocally") or test.get("executionInfo", {}).get("cachedRemotely"))
    result["child"] = next((r["started"].get("uuid") for r in rows if "started" in r), None)
    assert result["remote"] and not result["cached"]
    result["test_status"] = test["status"]
    stage = "reports"
    files = {f["name"]: f for f in test["testActionOutput"] if f["name"] in {"test.log", "test.xml"}}
    assert set(files) == {"test.log", "test.xml"}
    content = {}
    result["reports"] = {}
    for name, f in files.items():
        file = Path("bazel-testlogs",owner[2:].replace(":", "/"),name)
        data = file.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert f.get("digest", digest) == digest and int(f.get("length", len(data))) == len(data)
        content[name] = data
        result["reports"][name] = {"sha256": digest, "bytes": len(data)}
    cases = ET.fromstring(content["test.xml"]).findall(".//testcase")
    result["cases"] = [{"name": c.attrib["name"], "passed": all(c.find(t) is None for t in ("skipped", "error", "failure"))} for c in cases]
    assert {c["name"] for c in result["cases"]} == expected and len(cases) == len(expected)
    lines = content["test.log"].decode().splitlines()
    if natural:
        wire, decisions = [], None
        for line in lines:
            if "FIFO_WIRE rack_flight " in line:
                wire.extend(json.loads(line.split("FIFO_WIRE rack_flight ", 1)[1]))
            elif "FIFO_DECISIONS " in line:
                assert decisions is None
                decisions = json.loads(line.split("FIFO_DECISIONS ",1)[1])
        assert decisions and decisions["Mode"]=="rack_flight" and decisions["End"] == 3773123200
        assert len(wire)==decisions["Packets"] and len(wire)<50000 and all(len(x)==13 and x[0]==i for i,x in enumerate(wire))
        assert len(decisions["Flows"])==2 and all(f["Accepted"]>f["Delivered"]>0 for f in decisions["Flows"])
        first = next(x for x in wire if x[11])
        assert first[1] == 664297600 and first[3]==0 and first[4]==1472 and first[6]-decisions["Flows"][0]["Base"]==237140
        assert all(any(r["Kind"]=="setpipe_work" and r["ScanCalls"]>=2 and r["ScanACKs"]>0 for r in f["Decisions"]) for f in decisions["Flows"])
        serialized=gzip.compress(json.dumps({"wire":wire,"decisions":decisions},separators=(",",":")).encode(),mtime=0)
        (out/"recovery.json.gz").write_bytes(serialized)
        result["evidence"]={"bytes":len(serialized),"sha256":hashlib.sha256(serialized).hexdigest()}
    elif not unit:
        flight = re.compile(r"RACK_FLIGHT cc=(reno|cubic) first=(\d+) cap=(\d+) cwnd=(\d+) ssthresh=(\d+) rto=(\d+)")
        ack = re.compile(r"RACK_ACK cc=(reno|cubic) repeats=(\d+) new=(\d+)")
        result["flight"] = [m.groups() for line in lines if (m:=flight.search(line))]
        result["ack"] = [m.groups() for line in lines if (m:=ack.search(line))]
        errors=[v.text or "" for c in cases for v in c.findall("failure")]
        result["lost_pipe_diagnostic"] = any("known-lost segments after the tail SACK" in text for text in errors)
        result["limits"] = [m.groups() for line in lines if (m:=re.search(r"repaired (\d+) known-lost segments after the tail SACK, want (\d+)",line))]
        assert {r[0] for r in result["flight"]}=={"reno","cubic"} and len(result["flight"])==2
        result["gso"] = [m.groups() for line in lines if (m:=re.search(r"RACK_GSO accepted=(\d+) rejected=(\d+) unsent=(\d+) batch=2 errors=1", line))]
        if not code:
            assert len(result["gso"]) == 1 and len(set(result["gso"][0]))==1 and int(result["gso"][0][0])>0
            assert all(row[1:3]==("1","1") and row[5]=="0" for row in result["flight"])
            assert {(r[0],r[1],r[2]) for r in result["ack"]} == {("reno","0","2"),("cubic","0","2")}
    result["status"] = "PASS" if all(c["passed"] for c in result["cases"]) and not code and result["test_status"]=="PASSED" else "FAIL"
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as err:
    result["error"] = {"stage":stage,"kind":type(err).__name__}
finally:
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
sys.exit(int(result["status"]!="PASS"))
