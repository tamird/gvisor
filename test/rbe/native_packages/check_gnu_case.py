"""Check the original GNU TestRunner result for the helper's re-exec case."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

CASES = {"ExitTest.ExitAllThreads"}
TARGET = "//test/syscalls:exit_test_native"

def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_diagnostics(text):
    return [line[:450] for line in text.splitlines() if re.search(
        r'(?i)(error:|fatal:|warning:|FAILED:|command not found|No such file|GLIBC_|returned non-zero exit status)', line
    ) and not re.search(r'(?i)(auth|api.key|header=|https?:|token=|bazelrc:)', line)][:8]


def check_cases(work, result):
    result["exit"] = int((work / "syscalls.exit").read_text())
    result["diagnostics"] = safe_diagnostics((work / "syscalls.log").read_text(errors="replace"))
    events = [json.loads(line) for line in (work / "events").open()]
    result["invocation"] = next(e["started"]["uuid"] for e in events if "started" in e)
    end, = [e["finished"] for e in events if "finished" in e]
    result["bazel_exit"] = end["exitCode"].get("code", 0)
    result["build_finish"] = end.get("finishTime") or end.get("finishTimeMillis")
    tests = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label") == TARGET]
    result["test_results"] = [dict(status=t.get("status"), cached=t.get("cachedLocally", False),
                                   execution=t.get("executionInfo", {}).get("strategy")) for t in tests]
    if len(tests) != 1:
        raise ValueError(f"expected one original {TARGET} result, found {len(tests)}")
    evidence, = [o for o in tests[0]["testActionOutput"] if o["name"] == "test.xml"]
    xml = Path("bazel-testlogs/test/syscalls/exit_test_native/test.xml").read_bytes()
    result["xml_sha256"] = sha(xml)
    result["xml_bytes"] = len(xml)
    digest = evidence.get("digest")
    if digest:
        matches = digest == result["xml_sha256"] and int(evidence.get("length", -1)) == len(xml)
        result["xml_provenance"] = "BEP digest and length"
    else:
        uri = urlparse(evidence["uri"])
        matches = ((uri.scheme == "file" and Path(unquote(uri.path)).samefile(
            "bazel-testlogs/test/syscalls/exit_test_native/test.xml"))
            or (uri.scheme == "bytestream" and
                uri.path.endswith(f"/{result['xml_sha256']}/{len(xml)}")))
        result["xml_provenance"] = f"BEP {uri.scheme} output"
    if not matches:
        raise ValueError("native XML does not match the original TestRunner output")
    rows = []
    for case in ET.fromstring(xml).iter("testcase"):
        name = case.get("classname", "") + "." + case.get("name", "")
        passed = (case.get("status") == "run" and case.get("result") == "completed"
                  and not any(case.find(n) is not None for n in ("failure", "error", "skipped")))
        failed = any(case.find(n) is not None for n in ("failure", "error"))
        rows.append({"name": name, "passed": passed, "failed": failed, "seconds": case.get("time")})
    result["cases"] = rows
    (work / "public/native-test.xml").write_bytes(xml)
    result["verified"] = (result["exit"] == result["bazel_exit"] == 0 and tests[0]["status"] == "PASSED"
                          and tests[0].get("executionInfo", {}).get("strategy") == "local"
                          and not tests[0].get("cachedLocally") and len(rows) == len(CASES)
                          and {r["name"] for r in rows if r["passed"]} == CASES)


def main():
    work = Path(sys.argv[1])
    result = {"verified": False, "source": os.environ["PRODUCT"], "case": "ExitTest.ExitAllThreads"}
    try:
        check_cases(work, result)
    except (OSError, ValueError, StopIteration, KeyError, ET.ParseError) as exc:
        result["collection_error"] = f"{type(exc).__name__}: {exc}"[:450]
    result["verified_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    (work / "public/syscalls.json").write_text(json.dumps(result, indent=2))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
