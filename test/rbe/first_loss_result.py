"""Retain only the named TCP test results, measured events and report identities."""
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
suite = "TestVirtualTCPFirstCommonLoss"
modes = ("original", "without_hystart", "one_more_frame")
expected = {suite, *(f"{suite}/{mode}" for mode in modes)}
product = {"qualification/shared-fifo-first-loss-before": "9d9c3f0f479b3106b08d404f4f8604545f8d877d", "qualification/shared-fifo-first-loss-after": "431f54a4309b2c0d7df2294af61bc38465962c17"}[os.environ["GITHUB_REF_NAME"]]
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
fixture = {"qualification/shared-fifo-first-loss-before":"53001b900e4379c41072d1a8d0f469381b43591e", "qualification/shared-fifo-first-loss-after":"3482b120e000545b06fa6b656de403ed8e02a906"}[os.environ["GITHUB_REF_NAME"]]
result = {"status": "FAIL", "source": git("rev-parse", "HEAD"), "fixture": fixture, "product": product, "owner": owner, "native_exit": code, "modes": {}}
stage = "source"
try:
    assert result["source"] == os.environ["QUALIFICATION_COMMIT"] and git("rev-parse", fixture+"^") == product
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
    for line in content["test.log"].splitlines():
        if b"FIRST_LOSS {" not in line: continue
        event = json.loads(line.split(b"FIRST_LOSS ",1)[1]); mode=event["Scenario"]
        assert mode in modes and mode not in result["modes"]
        result["modes"][mode] = event
    assert set(result["modes"]) == set(modes) and all(c["passed"] for c in result["cases"]) and not code and result["test_status"] == "PASSED"
    result["status"] = "PASS"
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as err:
    result["error"] = {"stage":stage, "kind":type(err).__name__}
finally:
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
sys.exit(int(result["status"]!="PASS"))
