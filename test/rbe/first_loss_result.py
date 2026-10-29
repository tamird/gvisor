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
owner = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
suite = "TestWindowAfterSparseWrites"
modes = tuple(f"{cc}_{p}" for cc in ("cubic", "reno") for p in ("none", "dense", "spaced"))
expected = {suite, *(f"{suite}/{mode}" for mode in modes)}
product="431f54a4309b2c0d7df2294af61bc38465962c17"
fixture="2ca4a3522c53dd34739c99edd61221bc9f5ef7f7"
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
result = {"status": "FAIL", "source": git("rev-parse", "HEAD"), "fixture": fixture, "product": product, "owner": owner, "native_exit": code, "modes": {}}
stage = "source"
try:
    assert result["source"] == os.environ["QUALIFICATION_COMMIT"] and git("rev-parse", fixture+"^") == "b2e93f73e73da77205b3610a8ae88d1b5ed26389"
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
    assert result["remote"] and not result["cached"]
    result["test_status"] = test["status"]
    stage = "reports"
    files = {f["name"]: f for f in test["testActionOutput"] if f["name"] in {"test.log", "test.xml"}}
    assert set(files) == {"test.log", "test.xml"}
    content = {}
    result["reports"] = {}
    for name, f in files.items():
        file = Path("bazel-testlogs/pkg/tcpip/transport/tcp/test/e2e/tcp_test", name)
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
        if b"DEMAND_RESULT " not in line:
            continue
        record = json.loads(line.split(b"DEMAND_RESULT ", 1)[1])
        mode = f"{record['CC']}_{record['Profile']}"
        assert mode in modes and mode not in result["modes"]
        assert set(record) == set("CC Profile MSS PeerWindow PreCwnd PreThreshold Burst NextBurst RTT Gap Start End Retransmits Timeouts Samples".split())
        assert record["MSS"] == 32 and record["PeerWindow"] == 30000 and record["RTT"] == 10_000_000 and record["Timeouts"] == 0
        assert record["PreCwnd"] >= record["PreThreshold"] > 1 and record["Burst"]*32 < record["PeerWindow"]
        samples = record["Samples"] or []
        assert len(samples) == (0 if record["Profile"] == "none" else 64)
        gap = record["Gap"]
        assert gap == (90_000_000 if record["Profile"] == "spaced" else 0)
        for i, sample in enumerate(samples):
            assert set(sample) == set("At RTO Cwnd Threshold Flight Accepted Wire Acked".split())
            assert sample["At"] == (i+1)*(gap+record["RTT"]) and sample["RTO"] > gap+record["RTT"]
            assert sample["Flight"] == 1 and sample["Accepted"] == sample["Acked"] == sample["Wire"]
            assert sample["Wire"] == samples[0]["Wire"] + 32*i
        assert record["End"] == record["Start"] + len(samples)*(gap+record["RTT"]) + record["RTT"]
        result["modes"][mode] = record
    for cc in ("reno","cubic"):
        group = [result["modes"][cc+"_"+p] for p in ("dense","spaced","none")]
        assert all(len({m[k] for m in group}) == 1 for k in ("Start","PreCwnd","PreThreshold","Retransmits","Timeouts"))
        assert [(r["Acked"],r["Flight"]) for r in group[0]["Samples"]] == [(r["Acked"],r["Flight"]) for r in group[1]["Samples"]]
    assert set(result["modes"]) == set(modes) and all(c["passed"] for c in result["cases"]) and not code and result["test_status"] == "PASSED"
    result["status"] = "PASS"
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as err:
    result["error"] = {"stage":stage, "kind":type(err).__name__}
finally:
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
sys.exit(int(result["status"]!="PASS"))
