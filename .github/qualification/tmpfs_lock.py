"""Qualify the held and deliberately unguarded versions of one TMPFS call."""

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

WORK = Path(sys.argv[1])
TARGET = "//pkg/sentry/fsimpl/tmpfs:tmpfs_nogo"
SOURCE = Path("pkg/sentry/fsimpl/tmpfs/tmpfs.go")
CALL = "\t\t\td.inode.decLinksLocked(ctx)"
DECL = "// +checklocks:d.inode.fs.mu\nfunc (d *dentry) releaseChildrenLocked("
REPORT = {"source": os.environ["PRODUCT"], "parent": os.environ["ORIGINAL"],
          "machine": platform.machine(), "target": TARGET}


def save():
    (WORK / "result.json").write_text(json.dumps(REPORT, sort_keys=True, indent=2) + "\n")


def measure(phase):
    lines = SOURCE.read_text().splitlines()
    assert lines.count(CALL) == 1
    number = lines.index(CALL) + 1
    bep = WORK / (phase + ".bep")
    code = subprocess.run([WORK / "bazelisk", "test", "-c", "opt", "--jobs=4",
                           "--remote_cache=", "--remote_executor=", "--strategy=TestRunner=local",
                           "--test_output=errors", "--test_tag_filters=", "--build_tag_filters=",
                           "--nocache_test_results", "--runs_per_test=1", "--flaky_test_attempts=1",
                           "--test_sharding_strategy=disabled", "--build_event_json_file=" + str(bep), TARGET]).returncode
    row = REPORT[phase] = {"exit": code, "line": number, "results": []}
    save()
    if not bep.exists():
        return False
    for line in bep.open():
        event = json.loads(line)
        key = event.get("id", {}).get("testResult", {})
        if key.get("label") != TARGET or "testResult" not in event:
            continue
        test = event["testResult"]
        status = {"status": test["status"], "fresh": not test.get("cachedLocally", False) and
                  not test.get("executionInfo", {}).get("cachedRemotely", False)}
        row["results"].append(status)
        log = Path("bazel-testlogs/pkg/sentry/fsimpl/tmpfs/tmpfs_nogo/test.log")
        if not log.is_file():
            continue
        data = log.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        matched = False
        for output in test.get("testActionOutput", []):
            if output.get("name") == "test.log":
                uri = urlsplit(output.get("uri", ""))
                matched = output["digest"] == sha if output.get("digest") else (
                    uri.scheme == "file" and not uri.netloc and Path(unquote(uri.path)).resolve() == log.resolve())
        status.update(logMatches=matched, logSHA256=sha)
        if not matched:
            continue
        parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", data.decode(errors="replace"))
        evidence = {}
        for arch, block in zip(parts[1::2], parts[2::2]):
            error = rf"tmpfs\.go:{number}(?::[0-9]+)?:[^\n]*must hold [^\n]*to call decLinksLocked, but not held"
            evidence[arch] = {"passed": block.startswith("PASS"), "guardErrorAtCall": bool(re.search(error, block))}
        status["architectures"] = evidence
        save()
    if len(row["results"]) != 1:
        return False
    status, = row["results"]
    evidence = status.get("architectures", {})
    if not status["fresh"] or set(evidence) != {"amd64", "arm64"}:
        return False
    if phase == "guarded":
        return code == 0 and status["status"] == "PASSED" and all(v["passed"] for v in evidence.values())
    return code == 3 and status["status"] == "FAILED" and all(v["guardErrorAtCall"] for v in evidence.values())


original = SOURCE.read_bytes()
assert original.count(DECL.encode()) == 1
save()
guarded = measure("guarded")
if guarded:
    try:
        SOURCE.write_bytes(original.replace(DECL.encode(), b"func (d *dentry) releaseChildrenLocked("))
        REPORT["controlSourceSHA256"] = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
        REPORT["verified"] = measure("unguarded")
    finally:
        SOURCE.write_bytes(original)
save()
sys.exit(0 if REPORT.get("verified") else 1)
