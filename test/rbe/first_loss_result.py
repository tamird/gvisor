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
suite = "TestReceiveQueueOverBudget"
expected = {suite}
product = "1343dc381c63c720ab2d12f24371d04a959acfbe"
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
result = {"status": "FAIL", "source": git("rev-parse", "HEAD"), "product": product, "owner": owner, "native_exit": code}
stage = "source"
try:
    assert result["source"] == os.environ["QUALIFICATION_COMMIT"]
    assert git("rev-parse", "HEAD^") == product
    assert git("rev-parse", product + "^") == "3cd9a5d970293241934d039506f43ad55384c7fe"
    subprocess.run(["git", "diff", "--exit-code", product, "HEAD", "--", "pkg"], check=True)
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
    result["diagnostics"] = [line.strip()[:350] for line in content["test.log"].decode(errors="replace").splitlines() if "tcp_rack_test.go:" in line or "timed out" in line.lower() or "error running check" in line][:12]
    assert {c["name"] for c in result["cases"]} == expected and len(cases) == len(expected)
    assert all(c["passed"] for c in result["cases"])
    assert code == 0 and result["test_status"] == "PASSED"
    rows = re.findall(rb"receive queue: packets=(\d+) charged=(\d+) budget=(\d+)", content["test.log"])
    assert len(rows) == 3
    samples = [tuple(map(int,row)) for row in rows]
    assert [k for k,_,_ in samples] == [1,33,257] and {budget for _,_,budget in samples} == {4096}
    result.update(status="PASS", measurements=samples)
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as err:
    result["error"] = {"stage":stage,"kind":type(err).__name__}
finally:
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
sys.exit(int(result["status"]!="PASS"))
