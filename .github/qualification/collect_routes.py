"""Retain the remote target result and the three named route test outcomes."""

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

work = Path(os.environ["RUNNER_TEMP"]) / "route-sandbox"
label = "//runsc/sandbox:sandbox_test"
cases = {
    "TestCollectLinksAndRoutes_" + suffix: {}
    for suffix in ("SingleInterface", "MultipleInterfaces", "IPv6Disabled")
}
attempts = []
result = {
    "source": "ccc5942a19839ea09ca5939b1279859b2f81c5e7",
    "base": "53c458937842fb431ef351c39781850f5a817234",
    "label": label,
    "attempts": attempts,
    "cases": cases,
    "verified": False,
}
code = work / "exit"
if code.exists():
    result["exit"] = int(code.read_text())

events = work / "events"
if events.exists():
    with events.open() as stream:
        for line in stream:
            event = json.loads(line)
            if "started" in event:
                result["invocation"] = event["started"].get("uuid")
            key = event["id"]
            if "testResult" in event and key.get("testResult", {}).get("label") == label:
                attempt = event["testResult"]
                info = attempt.get("executionInfo", {})
                attempts.append({
                    "status": attempt["status"],
                    "remote": info.get("strategy") == "remote",
                    "cached": bool(attempt.get("cachedLocally") or info.get("cachedRemotely")),
                })
            if "testSummary" in event and key.get("testSummary", {}).get("label") == label:
                result["summary"] = event["testSummary"]["overallStatus"]

output = work / "output"
if output.exists():
    with output.open(errors="replace") as stream:
        for line in stream:
            match = re.search(r"--- (PASS|SKIP|FAIL): (TestCollectLinksAndRoutes_\w+) \(", line)
            if match and match[2] in cases:
                cases[match[2]]["go"] = match[1]
            for diagnostic in ("FDBasedLinks mismatch", "Defaultv4Gateway mismatch",
                               "Failed to create network namespace", "no matching execution platform"):
                if diagnostic in line:
                    result.setdefault("diagnostics", {})[diagnostic] = True

xml = Path("bazel-testlogs/runsc/sandbox/sandbox_test/test.xml")
if xml.is_file():
    try:
        nodes = ET.parse(xml).findall(".//testcase")
        result["xml_total"] = len(nodes)
        for node in nodes:
            name = node.get("name")
            if name in cases:
                marks = [tag for tag in ("skipped", "failure", "error") if node.find(tag) is not None]
                if node.get("status") in ("notrun", "skipped") or node.get("result") == "suppressed":
                    marks.append("skipped")
                cases[name]["xml"] = (marks or ["PASS"])[0]
    except ET.ParseError:
        result["xml_parse_error"] = True

result["verified"] = (
    result.get("exit") == 0
    and result.get("summary") == "PASSED"
    and attempts == [{"status": "PASSED", "remote": True, "cached": False}]
    and all(case and all(value == "PASS" for value in case.values()) for case in cases.values())
)
(work / "result.json").write_text(json.dumps(result, indent=2) + "\n")
