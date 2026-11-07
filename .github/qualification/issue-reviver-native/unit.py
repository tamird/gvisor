"""Bind actual Go PASS lines to the native Bazel TestResult's own test.log."""

import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

tmp = Path(os.environ["RUNNER_TEMP"])
events = [json.loads(s) for s in (tmp / "unit.bep").read_text().splitlines()]
results = [e for e in events if "testResult" in e.get("id", {})]
if len(results) != 1:
    raise ValueError(f"expected one test owner, got {len(results)}")
result = results[0]["testResult"]
name = results[0]["id"]["testResult"]["label"]
if not name.startswith("//tools/github/reviver:reviver_test") or result.get("status") != "PASSED" or result.get("cachedLocally"):
    raise ValueError((name, result.get("status"), result.get("cachedLocally")))
outputs = {}
files = {}
for basename in ("test.log", "test.xml"):
    (entry,) = [x for x in result["testActionOutput"] if x["name"] == basename]
    uri = urlsplit(entry["uri"])
    if uri.scheme != "file":
        raise ValueError(f"expected local {basename}, got {uri.scheme}")
    data = Path(unquote(uri.path)).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if entry.get("digest", digest) != digest or int(entry.get("length", len(data))) != len(data):
        raise ValueError(f"configured {basename} identity mismatch")
    outputs[basename] = {"size": len(data), "sha256": digest}
    files[basename] = data
xml = ET.fromstring(files["test.xml"])
if any(e.tag in {"failure", "error", "skipped"} for e in xml.iter()):
    raise ValueError("unexpected test.xml failure or skip")
cases = re.findall(r"(?m)^\s*--- PASS: (Test\S+) \(", files["test.log"].decode())
if {name for name in cases if "/" not in name} != {"TestParseIssueNo", "TestProcessLine"} or len(cases) != 6:
    raise ValueError(f"unexpected named PASS rows: {cases}")
(tmp / "unit-result.json").write_text(json.dumps({
    "label": name, "cases": cases, "xml_case_count": sum(1 for _ in xml.iter("testcase")),
    "outputs": outputs, "status": "PASSED",
}, indent=2) + "\n")
