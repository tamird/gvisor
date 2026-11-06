"""Keep bounded, source-owned case outcomes from the two sandboxexec tests."""

import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from urllib.parse import urlparse, unquote

WORK = Path(os.environ["RUNNER_TEMP"]) / "sandboxexec-owners"
GO = "//examples/sandboxexec/go/docprocessor:docprocessor_test"
PYTHON = "//sandboxexec/sandbox/python:sandbox_py_test"
go_source = Path("examples/sandboxexec/go/docprocessor/docprocessor_test.go").read_text()
py_source = Path("sandboxexec/sandbox/python/tests/sandbox_test.py").read_text()
go_names = set(re.findall(r"^func (Test\w+)\(t ", go_source, re.M))
go_names.update({"TestProcessDocuments_NetworkModes/host", "TestProcessDocuments_NetworkModes/none"})
py_names = set(re.findall(r"^  def (test_\w+)\(", py_source, re.M))
assert len(py_names) == 39 and len(go_names) == 9
TERMS = (
    "read-only file system", "device or resource busy", "operation not permitted",
    "permission denied", "no such file or directory", "connection refused", "connection reset by peer",
    "creating container", "failed to start", "failed to create", "ignored incomplete",
    "/sys/fs/cgroup", "cgroup.procs", "cgroup.subtree_control", "null-netns", "snapshot", "restore",
    "timed out", "deadline exceeded", "SIGKILL", "SIGSEGV", "panic:", "external/",
    "checksum mismatch", "sandbox_options", "assertFalse", "assertTrue", "test failed",
)
STATUS = {"PASSED", "FAILED", "TIMEOUT", "REMOTE_FAILURE", "FAILED_TO_BUILD", "NO_STATUS", "INCOMPLETE"}

def read_log(owner, event):
    entries = [item for item in event.get("testActionOutput", []) if item["name"] == "test.log"]
    candidates = []
    expected = None
    for item in entries:
        url = urlparse(item.get("uri", ""))
        if url.scheme == "file" and not url.netloc:
            candidates.append(Path(unquote(url.path)))
        elif url.scheme == "bytestream":
            match = re.search(r"/blobs/([0-9a-f]{64})/(\d+)$", url.path)
            if match:
                expected = (match[1], int(match[2]))
    # A transitioned test's downloaded log may be outside the default
    # bazel-testlogs symlink. Match its original BEP digest before reading it.
    relative = owner[2:].replace(":", "/") + "/test.log"
    candidates.append(Path("bazel-testlogs") / relative)
    if expected is not None:
        candidates.extend(Path("bazel-out").glob("*/testlogs/" + relative))
    for path in candidates:
        if path.is_file() and path.stat().st_size < 4_000_000:
            raw = path.read_bytes()
            if expected is None or expected == (hashlib.sha256(raw).hexdigest(), len(raw)):
                return raw
    return b""

def diagnose(raw):
    lines = raw.decode(errors="replace").splitlines()
    source_files = {"sandbox_test.py", "sandbox.py", "docprocessor.go", "docprocessor_test.go",
                    "sandbox.go", "namespace.go", "tempfile.py", "shutil.py"}
    terms = {term: sum(term.lower() in line.lower() for line in lines) for term in TERMS}
    locations = set()
    for line in lines:
        for name, number in re.findall(r'([\w]+\.(?:py|go))(?:", line |:)(\d+)\b', line):
            if name in source_files:
                locations.add((name, int(number)))
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "terms": {k: v for k, v in terms.items() if v}, "locations": sorted(locations)[:100]}

def cases(owner, raw):
    text = raw.decode(errors="replace")
    if owner == GO:
        return {name: status for status, name in re.findall(r'(?m)^\s*--- (PASS|FAIL|SKIP): ([\w/]+)', text)
                if name in go_names}
    count = re.search(r"(?m)^Ran (\d+) tests? in ", text)
    skipped = re.search(r"skipped=(\d+)", text)
    failures = [name for name in re.findall(r"(?m)^(?:ERROR|FAIL): (test_\w+)\b", text) if name in py_names]
    return {"ran": int(count[1]) if count else None, "skipped": int(skipped[1]) if skipped else 0,
            "ok": re.search(r"^OK(?: \(skipped=\d+\))?$", text, re.M) is not None,
            "failed": failures, "source_methods": sorted(py_names)}

report = {"source": "c0511e1cd383b0f2870c15a104222f0d285aac6f", "owners": {}, "summaries": {},
          "exit": int((WORK / "exit").read_text())}
for line in (WORK / "run.bep").open():
    event = json.loads(line)
    if "started" in event:
        report["invocation"] = str(uuid.UUID(event["started"]["uuid"]))
    for kind in ("testSummary", "testResult"):
        if kind not in event:
            continue
        owner = event["id"][kind]["label"]
        assert owner in (GO, PYTHON)
        result = event[kind]
        if kind == "testSummary":
            status = result.get("overallStatus", "NO_STATUS")
            report["summaries"][owner] = status if status in STATUS else "other"
            continue
        info = result.get("executionInfo", {})
        raw = read_log(owner, result)
        strategy = info.get("strategy")
        report["owners"][owner] = {
            "status": result["status"] if result["status"] in STATUS else "other",
            "strategy": strategy if strategy in ("remote", "local", "linux-sandbox") else None,
            "cached": bool(result.get("cachedLocally") or info.get("cachedRemotely")),
            "diagnostics": diagnose(raw), "cases": cases(owner, raw),
        }
if (WORK / "run.console").is_file():
    report["console"] = diagnose((WORK / "run.console").read_bytes()[-256_000:])
owners = report["owners"]
report["passed"] = (
    report["exit"] == 0 and set(owners) == {GO, PYTHON}
    and all(v["strategy"] == "remote" and v["status"] == "PASSED" and not v["cached"] for v in owners.values())
    and owners[GO]["cases"] == dict.fromkeys(go_names, "PASS")
    and owners[PYTHON]["cases"]["ran"] == len(py_names) and owners[PYTHON]["cases"]["ok"]
    and owners[PYTHON]["cases"]["skipped"] == 1
)
(WORK / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
