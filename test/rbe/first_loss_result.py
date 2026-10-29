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
parts = [part.strip("^$") for part in os.environ["QUALIFICATION_TEST_FILTER"].split("/")]
suite=parts[0]
selected=parts[1:] or ["same","enable","disable","smaller"]
expected={suite, *(suite+"/"+kind for kind in selected)}
fixture = "cfe64e4a5129d3ef84f2e7465288e0337ccdfbef"
product = "177ac485143602b23d57d308ba401d6b813e3035"
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
result = {"status": "FAIL", "source": git("rev-parse", "HEAD"), "fixture": fixture, "product": product, "owner": owner, "native_exit": code, "modes": {}}
stage = "source"
try:
    assert result["source"] == os.environ["QUALIFICATION_COMMIT"] and git("rev-parse", fixture+"^") == "ac7b4e0fdc7dc4674453438e5e15fc069895620c"
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
    result["prefix"]=[m.groups() for line in lines if (m:=re.search(r"GSO_PREFIX mss=(\d+) nxt=(\d+) outstanding=(\d+)",line))]
    result["suffix"]=[m.groups() for line in lines if (m:=re.search(r"GSO_SUFFIX mode=(ack|timeout|restore) mss=(\d+) accepted=(\d+) retries=(\d+)",line))]
    allowed=("sender counts rejected bytes","restored sender did not transmit unsent bytes","timed out", "route errors", "unexpected further timeout","rejected suffix","no sender observation","restored sender returned wrong range")
    result["failure_kinds"]=sorted({x for line in lines for x in allowed if x.lower() in line.lower()})
    result["empty"] = [m.groups() for line in lines if (m:=re.search(r"GSO_EMPTY mode=(full|retry|restore) bytes=(\d+) network_rto=(\d+)",line))]
    result["empty_timeout"] = [m.groups() for line in lines if (m:=re.search(r"GSO_EMPTY_TIMEOUT at_deadline=(true|false) network_rto=(\d+)",line))]
    result["failure_kinds"].extend(sorted({x for line in lines for x in ("no restored retry","user timeout did not expire","unexpected extra wire output","unexpected terminal error","wrong restored range","initial errors","unexpected wire output","timed out") if x in line.lower()}))
    result["restore"]=[m.groups() for line in lines if (m:=re.search(r"GSO_RESTORE mode=(same|enable|disable|smaller) bytes=(\d+) frames=(\d+)",line))]
    if any("integer divide by zero" in line for line in lines): result["failure_kinds"].append("integer divide by zero")
    if not code:
        if suite=="TestGSOAfterRestore":
            assert {r[0] for r in result["restore"]}==set(selected) and all(int(n)>0 and int(k)>=2 for m,n,k in result["restore"])
        elif suite=="TestPartialGSOProgress":
            assert len(result["prefix"])==1 and all(mss==nxt and int(mss)>0 and fl=="1" for mss,nxt,fl in result["prefix"])
            assert len(result["suffix"])==1 and all(m=="restore" and int(n)==2*int(mss) and rt=="0" for m,mss,n,rt in result["suffix"])
        else:
            assert len(result["empty"])==1 and all(m=="restore" and int(n)>0 and rt=="0" for m,n,rt in result["empty"])
    result["status"] = "PASS" if all(c["passed"] for c in result["cases"]) and not code and result["test_status"]=="PASSED" else "FAIL"
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as err:
    result["error"] = {"stage":stage,"kind":type(err).__name__}
finally:
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
sys.exit(int(result["status"]!="PASS"))
