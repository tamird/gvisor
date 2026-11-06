"""Collect one original timed packet trace and its Bazel result."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from xml.etree import ElementTree

from qualification_report import xml_cases

root = Path(os.environ["RUNNER_TEMP"]) / "warm-ack"
phase, source, code = sys.argv[1:]
code = int(code)
case = "TestDelayedACKAfterQuickACKs"
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
    for first, ack, ms in re.findall(r"WARM_ACK warmup=16 first=(\d+) cumulative=(\d+) delay_ms=(\d+)", log.read_text()):
        name = {1: "InOrder", 2: "FilledGap"}[(int(ack) - int(first)) % 2**32]
        assert name not in cases
        node = leaves[case + "/" + name]
        assert node.find("skipped") is None and node.find("error") is None
        cases[name] = {"first": int(first), "ack": int(ack), "ms": int(ms), "passed": node.find("failure") is None}
    assert set(cases) == {"InOrder", "FilledGap"} and not out["cached"] and out["xml"]["skipped"] == 0
    assert code in (0, 3) and (code == 0) == (out["result"] == "PASSED")
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
