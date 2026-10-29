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
owner = "//pkg/tcpip/tests/integration:virtual_tcp_test"
suite = "TestVirtualTCPHyStartPacedPath"
modes = ("cubic","cubic_without_ack_train","reno")
expected = {suite, *(f"{suite}/{mode}" for mode in modes)}
product="431f54a4309b2c0d7df2294af61bc38465962c17"
fixture="68e376f7c2ae813c3cdee8a364230f167b77cd14"
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
result = {"status": "FAIL", "source": git("rev-parse", "HEAD"), "fixture": fixture, "product": product, "owner": owner, "native_exit": code, "modes": {}}
stage = "source"
try:
    assert result["source"] == os.environ["QUALIFICATION_COMMIT"] and git("rev-parse", fixture+"^") == "f4e96e725fe7e8761e8ce51da5190189909e8590"
    subprocess.run(["git","merge-base","--is-ancestor",product,fixture],check=True)
    subprocess.run(["git","merge-base","--is-ancestor",fixture,"HEAD"],check=True)
    subprocess.run(["git","diff","--exit-code",fixture,"HEAD","--","pkg"],check=True)
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
    assert result["remote"] or result["cached"]
    result["test_status"] = test["status"]
    stage = "reports"
    files = {f["name"]: f for f in test["testActionOutput"] if f["name"] in {"test.log", "test.xml"}}
    assert set(files) == {"test.log", "test.xml"}
    content = {}
    result["reports"] = {}
    for name, f in files.items():
        file = Path("bazel-testlogs/pkg/tcpip/tests/integration/virtual_tcp_test", name)
        data = file.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert f.get("digest", digest) == digest and int(f.get("length", len(data))) == len(data)
        content[name] = data
        result["reports"][name] = {"sha256": digest, "bytes": len(data)}
    cases = ET.fromstring(content["test.xml"]).findall(".//testcase")
    result["cases"] = [{"name": c.attrib["name"], "passed": all(c.find(t) is None for t in ("skipped", "error", "failure"))} for c in cases]
    assert {c["name"] for c in result["cases"]} == expected and len(cases) == len(expected)
    stage = "events"
    evidence = {mode:{"wire": [], "measurements": None} for mode in modes}
    for line in content["test.log"].splitlines():
        if b"STARTUP_WIRE " in line:
            mode, chunk = line.split(b"STARTUP_WIRE ",1)[1].split(b" ",1)
            rows = json.loads(chunk)
            assert 1<=len(rows)<=64 and all(len(row)==13 and all(type(x) is int for x in row) for row in rows)
            evidence[mode.decode()]["wire"].extend(rows)
        elif b"STARTUP_PATH " in line:
            measure=json.loads(line.split(b"STARTUP_PATH ",1)[1]); row=evidence[measure["Mode"]]
            assert row["measurements"] is None
            row["measurements"]=measure
    for mode,v in evidence.items():
        rows,m=v["wire"],v["measurements"]
        assert m and len(rows)==m["Packets"] and 0<len(rows)<=50000
        assert (m["Gate"],m["End"],m["Rate"],m["Capacity"],m["Delay"])==(250000000,3850000000,2500000,1500000,75000000)
        assert [x["At"] for x in m["Points"]]==list(range(m["Gate"],m["End"]+1,25000000))
        assert all(x["Accepted"]>x["Delivered"] and x["Accepted"]<(16<<20) for x in m["Points"])
        assert all(r[0]==i and (not i or r[1]>=rows[i-1][1]) and r[11:]==[0,0] and r[10]==r[9]+m["Delay"] for i,r in enumerate(rows))
        assert m["Timeouts"]==0 and m["Retransmits"]==0
        result["modes"][mode]={"events":len(rows),"samples":len(m["Points"]),"delivered":m["Points"][-1]["Delivered"],"ACK_train":m["Train"],"delay_exit":m["DelayReason"],"exit_bits":m["Sender"]["HyStartExit"]}
    assert len({v["measurements"]["Base"] for v in evidence.values()})==1
    blob=json.dumps(evidence,separators=(",",":")).encode()
    serialized=gzip.compress(blob,mtime=0)
    (out/"startup.json.gz").write_bytes(serialized)
    result["evidence"]={"bytes":len(serialized),"sha256":hashlib.sha256(serialized).hexdigest()}
    assert set(result["modes"])==set(modes) and all(c["passed"] for c in result["cases"]) and not code and result["test_status"]=="PASSED"
    result["status"] = "PASS"
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as err:
    result["error"] = {"stage":stage, "kind":type(err).__name__}
finally:
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
sys.exit(int(result["status"]!="PASS"))
