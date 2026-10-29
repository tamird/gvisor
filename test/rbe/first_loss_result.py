"""Retain only the named TCP test results, measured events and report identities."""
import hashlib
import json
from urllib.parse import unquote, urlparse
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

out, code = Path(sys.argv[1]), int(sys.argv[2])
owner = os.environ["QUALIFICATION_TARGETS"]
suite = "TestGSOTailLossProbe"
expected = {suite, *(suite+"/"+name for name in ("full", "short"))}
stock, fixed, parent = "54dd721a4650c8c5f6afb72dfcc590442b4a13a0", "4c31b4fc4c319eb6aa71cda9bc1b8365739c20d6", "26a3cc72d469db14c5cdf7ddcf101fe1ac71592d"
product = os.environ["QUALIFICATION_COMMIT"]
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
result = {"status": "FAIL", "source": git("rev-parse", "HEAD"), "product": product, "owner": owner, "native_exit": code}
stage = "source"
try:
    assert result["source"] == os.environ["QUALIFICATION_COMMIT"]
    assert product in {stock, fixed} and git("rev-parse", "HEAD^") == parent
    assert git("diff", "--name-only", stock, fixed).splitlines() == ["pkg/tcpip/transport/tcp/rack.go"]
    assert not git("status", "--porcelain", "--untracked-files=no")
    assert git("diff", "--name-only", parent, stock).splitlines() == ["pkg/tcpip/transport/tcp/test/e2e/tcp_rack_test.go"]
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
    assert set(files) == {"test.log", "test.xml"} and len([f for f in test["testActionOutput"] if f["name"] in files]) == 2
    content = {}
    result["reports"] = {}
    for name, f in files.items():
        file = Path("bazel-testlogs",owner[2:].replace(":", "/"),name)
        data = file.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        uri = urlparse(f["uri"])
        if uri.scheme == "bytestream":
            assert uri.path.endswith(f"/blobs/{digest}/{len(data)}")
        else:
            assert uri.scheme == "file" and file.resolve() == Path(unquote(uri.path)).resolve()
        if f.get("digest"):
            assert f["digest"] == digest
        if "length" in f:
            assert int(f["length"]) == len(data)
        content[name] = data
        result["reports"][name] = {"sha256": digest, "bytes": len(data)}
    cases = ET.fromstring(content["test.xml"]).findall(".//testcase")
    result["cases"] = [{"name": c.attrib["name"], "passed": all(c.find(t) is None for t in ("skipped", "error", "failure"))} for c in cases]
    result["diagnostics"] = [line.strip()[:350] for line in content["test.log"].decode(errors="replace").splitlines() if "tcp_rack_test.go:" in line or "timed out" in line.lower() or "error running check" in line][:12]
    assert {c["name"] for c in result["cases"]} == expected and len(cases) == len(expected)
    assert not any(c.find(t) is not None for c in cases for t in ("skipped", "error"))
    if product == fixed:
        assert all(c["passed"] for c in result["cases"])
        assert code == 0 and result["test_status"] == "PASSED"
        result.update(status="PASS", observation="Both first probes selected the last segment")
    else:
        assert code == 3 and result["test_status"] == "FAILED"
        assert all(c.find("failure") is not None for c in cases)
        log = content["test.log"].decode()
        deltas = []
        for c in cases:
            if c.attrib["name"] == suite:
                continue
            failure = c.find("failure")
            diagnostic = " ".join(failure.attrib.values()) + " " + "".join(failure.itertext())
            [(got, want)] = set(re.findall(r"Bad sequence number, got = (\d+), want = (\d+)", diagnostic))
            assert f"Bad sequence number, got = {got}, want = {want}" in log
            delta = (int(want) - int(got)) % (1 << 32)
            assert delta > 0 and delta % 2 == 0
            deltas.append(delta)
            mss = delta // 2
            lengths = set(re.findall(r"Bad payload length, got = (\d+), want = (\d+)", diagnostic))
            if c.attrib["name"] == suite + "/short":
                assert lengths == {(str(20 + mss), str(20 + mss // 2))}
                assert f"Bad payload length, got = {20+mss}, want = {20+mss//2}" in log
            else:
                assert not lengths
            actual_diagnostics = re.findall(r"\.go:\d+: ([^\n]+)", diagnostic)
            assert all("Bad sequence number" in line or "Bad payload length" in line for line in actual_diagnostics)
        assert len(deltas) == 2 and deltas[0] == deltas[1]
        result.update(status="PASS", observation="Both first probes repeated the leading MSS", mss=deltas[0]//2)
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as err:
    result["error"] = {"stage":stage,"kind":type(err).__name__}
finally:
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
sys.exit(int(result["status"]!="PASS"))
