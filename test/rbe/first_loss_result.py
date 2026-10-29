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
expected = ["TestRFC8985LostRepair", "TestRFC8985LostRepair/repeat_tail", "TestRFC8985LostRepair/sack_repair", "TestRFC8985LostRepair/timestamp_peer"]
product = '8d28e0cabbb7792acea2ead0861e629074ffa6f0'
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
result = {"status": "FAIL", "source": git("rev-parse", "HEAD"), "product": product, "owner": owner, "native_exit": code}
stage = "source"
try:
    assert result["source"] == os.environ["QUALIFICATION_COMMIT"]
    assert git("rev-parse", "HEAD^") == product
    assert git("rev-parse", product + "^") == "03e72471a6e61beecec932f838a991a1585eae50"
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
    trace = result["trace"] = []
    allowed = {"begin", "initial", "wire", "ack", "mark", "eligible", "cc_loss", "cc_rto", "pipe", "state", "counters", "reorder_timer", "pto", "end", "fallback"}
    names = {"repeat_tail", "sack_repair", "timestamp_peer", "original", "repair", "again", "after_timeout", "originals", "repairs", "complete"}
    for line in content["test.log"].decode(errors="replace").splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[0] == "RR":
            assert parts[1] in allowed
            data = []
            for item in parts[2:]:
                if item in {"true", "false"}:
                    data.append(item == "true")
                elif item in names:
                    data.append(item)
                else:
                    assert re.fullmatch(r"-?[0-9]{1,22}", item), parts[1]
                    data.append(int(item))
            trace.append([parts[1], *data])
    assert trace
    cases = ET.fromstring(content["test.xml"]).findall(".//testcase")
    result["cases"] = [{"name": c.attrib["name"], "passed": all(c.find(t) is None for t in ("skipped", "error", "failure"))} for c in cases]
    assert {c["name"] for c in result["cases"]} == set(expected) and len(cases) == len(expected)
    if owner.endswith(":tcp_nogo"):
        log = content["test.log"].decode()
        result["target_arch_mentions"] = [arch for arch in ("arm64", "amd64") if arch in log]
        assert len(result["target_arch_mentions"]) == 2
    if not code:
        assert [row[1] for row in trace if row[0] == "begin"] == ["repeat_tail", "sack_repair", "timestamp_peer"]
        assert [row[1] for row in trace if row[0] == "end"] == ["repeat_tail", "sack_repair", "timestamp_peer"]
        assert not any(row[0] == "pto" for row in trace)
        assert len([row for row in trace if row[0]=="counters"]) == 3
    result["status"] = "PASS" if all(c["passed"] for c in result["cases"]) and not code and result["test_status"]=="PASSED" else "FAIL"
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as err:
    result["error"] = {"stage":stage,"kind":type(err).__name__}
finally:
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
sys.exit(int(result["status"]!="PASS"))
