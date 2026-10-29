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
suite = "TestVirtualTCPRecoveryDecision"
arms = {
    "qualification/shared-fifo-recovery-before": ("9d9c3f0f479b3106b08d404f4f8604545f8d877d","14ba4f0ebc8c74a744467e2429190a5afb289980","c0ca76288702841c823138b5b1cb232dd47196d8",("original",)),
    "qualification/shared-fifo-recovery-after": ("431f54a4309b2c0d7df2294af61bc38465962c17","024203e444c1f5a0985d85f2cf41a29ac948bd8b","f4e96e725fe7e8761e8ce51da5190189909e8590",("original","old_entry_gate")),
}
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
product, fixture, parent, modes = arms[os.environ["GITHUB_REF_NAME"]]
expected = {suite, *(f"{suite}/{mode}" for mode in modes)}
result = {"status": "FAIL", "source": git("rev-parse", "HEAD"), "fixture": fixture, "product": product, "owner": owner, "native_exit": code, "modes": {}}
stage = "source"
try:
    assert result["source"] == os.environ["QUALIFICATION_COMMIT"] and git("rev-parse", fixture+"^") == parent
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
    evidence = {mode:{"wire": [], "decisions": None} for mode in modes}
    for line in content["test.log"].splitlines():
        if b"FIFO_WIRE " in line:
            mode, chunk = line.split(b"FIFO_WIRE ",1)[1].split(b" ",1)
            rows=json.loads(chunk)
            assert 1<=len(rows)<=64 and all(len(row)==13 and all(type(x) is int for x in row) for row in rows)
            evidence[mode.decode()]["wire"].extend(rows)
        elif b"FIFO_DECISIONS " in line:
            decisions=json.loads(line.split(b"FIFO_DECISIONS ",1)[1])
            record=evidence[decisions["Mode"]]; assert record["decisions"] is None
            record["decisions"]=decisions
    for mode, record in evidence.items():
        rows, decisions = record["wire"],record["decisions"]
        assert decisions and len(rows)==decisions["Packets"] and len(decisions["Flows"])==2
        assert all(row[0]==i for i,row in enumerate(rows)) and 0<len(rows)<=50000
        assert all(0<=row[1]<=decisions["End"] and (not i or rows[i-1][1]<=row[1]) for i,row in enumerate(rows))
        for f in decisions["Flows"]:
            assert f["Accepted"]>=f["Delivered"]>0 and f["Base"]>0
            assert all(0<=r["At"]<=decisions["End"] for r in f["Decisions"] or [])
        first=next(row for row in rows if row[11])
        assert first[1]==664297600 and first[3]==0 and first[4]==1472 and (first[6]-decisions["Flows"][0]["Base"])%2**32==237140
        result["modes"][mode]={"wire": len(rows), "end":decisions["End"], "rto_during_recovery":decisions["First"], "events": [len(f["Decisions"] or []) for f in decisions["Flows"]]}
    blob=json.dumps(evidence,separators=(",",":")).encode()
    serialized=gzip.compress(blob,mtime=0)
    (out/"recovery.json.gz").write_bytes(serialized)
    result["evidence"]={"bytes":len(serialized),"sha256":hashlib.sha256(serialized).hexdigest()}
    assert set(result["modes"])==set(modes) and all(c["passed"] for c in result["cases"]) and not code and result["test_status"]=="PASSED"
    result["status"] = "PASS"
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as err:
    result["error"] = {"stage":stage, "kind":type(err).__name__}
finally:
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
sys.exit(int(result["status"]!="PASS"))
