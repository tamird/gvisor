"""Collect one original timed packet trace and its Bazel result."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from xml.etree import ElementTree

from qualification_report import xml_cases

root = Path(os.environ["RUNNER_TEMP"]) / "ack-write"
phase, source, code = sys.argv[1:]
code = int(code)
case = "TestPiggybackACKAfterLinkWrite"
owner = "//pkg/tcpip/transport/tcp/test/e2e:tcp_sack_test"
out = {"phase": phase, "source": source, "exit": code, "status": "INCOMPLETE"}
try:
    results = []
    failures = []
    for line in (root / (phase + ".bep")).open():
        e = json.loads(line)
        if "testResult" in e and e["id"].get("testResult", {}).get("label") == owner:
            results.append(e["testResult"])
        action = e.get("action", {})
        if action and not action.get("success") and len(failures) < 8:
            ident = e["id"].get("actionCompleted", {})
            failures.append([ident.get("label"), action.get("type"), action.get("exitCode")])
    out["failed_actions"] = failures
    out["results"] = len(results)
    result, = results
    info = result.get("executionInfo", {})
    out.update(strategy=info.get("strategy"), cached=result.get("cachedLocally", False) or info.get("cachedRemotely", False),
               result=result["status"])
    directory = Path("bazel-testlogs/pkg/tcpip/transport/tcp/test/e2e/tcp_sack_test")
    xml, log = directory / "test.xml", directory / "test.log"
    out["xml"] = xml_cases(xml)
    for file in (log, xml):
        out[file.name] = hashlib.sha256(file.read_bytes()).hexdigest()
    leaves = {node.get("name"): node for node in ElementTree.parse(xml).iter("testcase")}
    cases = out["cases"] = {}
    pattern = (r"PIGGYBACK_ACK rejected=(true|false) pure_before_write=(true|false) response_on_wire=(true|false) "
               r"send_errors=(\d+) pure_at_40ms=(true|false) established=true")
    for rejected, early, payload, failures, late in re.findall(pattern, log.read_text()):
        name = "Rejected=" + rejected
        assert name not in cases
        node = leaves[case + "/" + name]
        assert node.find("skipped") is None and node.find("error") is None
        cases[name] = {"rejected": rejected == "true", "early_ack": early == "true", "response": payload == "true",
                       "send_errors": int(failures), "ack_at_40ms": late == "true",
                       "established": True, "passed": node.find("failure") is None}
    assert set(cases) == {"Rejected=false", "Rejected=true"} and not out["cached"] and out["xml"]["skipped"] == 0
    for row in cases.values():
        assert row["early_ack"] == (phase == "base")
        assert row["response"] != row["rejected"] and row["send_errors"] == int(row["rejected"])
        assert not row["ack_at_40ms"]
        assert row["passed"] == (phase == "base" or not row["rejected"])
    assert (code, out["result"]) == ((0, "PASSED") if phase == "base" else (3, "FAILED"))
    assert out["strategy"] == "linux-sandbox"
    out["status"] = "OBSERVED"
except (AssertionError, ValueError, KeyError, OSError) as exc:
    out["projection_error"] = type(exc).__name__
    text = (root / (phase + ".console")).read_text(errors="replace")
    out["diagnostics"] = [s for s in ("undefined:", "missing strict dependencies", "killed", "No space left",
                                      "no such package", "timed out", "GoStandardLibraryAnalysis") if s in text]
    # Include the packet test diagnostics when a packet assertion
    # fires before the numeric trace is complete.
    testlog = Path("bazel-testlogs/pkg/tcpip/transport/tcp/test/e2e/tcp_sack_test/test.log")
    if out.get("results") == 1 and testlog.is_file():
        out["packet_errors"] = [
            line.strip()[:280] for line in testlog.read_text(errors="replace").splitlines()
            if re.search(r"(?:tcp_sack_test|context|checker)\.go:\d+: ", line)
            and not re.search(r"(?i)token|credential|https?://|authorization", line)
        ][:8]
finally:
    (root / (phase + ".json")).write_text(json.dumps(out, indent=2) + "\n")
sys.exit(out["status"] != "OBSERVED")
