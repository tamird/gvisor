"""Bind the unit outcome to its configured Bazel TestResult and test.xml."""

import hashlib
import json
import os
from pathlib import Path
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
(xml,) = [x for x in result["testActionOutput"] if x["name"] == "test.xml"]
uri = urlsplit(xml["uri"])
if uri.scheme != "file":
    raise ValueError(f"expected a local test.xml from the native runner, got {uri.scheme}")
data = Path(unquote(uri.path)).read_bytes()
digest = hashlib.sha256(data).hexdigest()
if xml.get("digest", digest) != digest or int(xml.get("length", len(data))) != len(data):
    raise ValueError("configured XML identity mismatch")
cases = [c.get("name") for c in ET.fromstring(data).iter("testcase")]
if not {"TestParseIssueNo","TestProcessLine"} <= set(cases) or any(e.tag in {"failure", "error", "skipped"} for e in ET.fromstring(data).iter()):
    raise ValueError(f"unexpected unit result {cases}")
(tmp / "unit-result.json").write_text(json.dumps({"label": name, "cases": cases, "xml_size": len(data), "xml_sha256": digest, "status": "PASSED"}, indent=2) + "\n")
