"""Recover the original ARM64 test's metadata without executing any build."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile


INVOCATION = "704bd367-4cfc-41ca-83d2-6ee550d1496d"
SOURCE = "0afd5b37e31824e0299c44235ef4cb58af0358ce"
TARGET = "//tools/nvidia_driver_differ:driver_ast_parser_test"
BINARY = {"hash": "c39e7d7ceff9cc255fcfe35aea6271f64db9a8da2d5f9e595508e87794550d0d", "sizeBytes": "224953640"}
PROPERTIES = {
    "Arch": "arm64",
    "OSFamily": "linux",
    "workload-isolation-type": "oci",
    "container-image": "docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
}
temp = Path(os.environ["RUNNER_TEMP"])
dest = temp / "parser-metadata"
dest.mkdir(exist_ok=True)
report = {"status": "INCOMPLETE", "phase": "read-original-receipt", "source": SOURCE,
          "originalRun": 41265512208, "originalArtifact": 13067164785,
          "invocation": INVOCATION, "target": TARGET, "metadataOnly": True,
          "startedAt": datetime.now(timezone.utc).isoformat()}


def save():
    (dest / "result.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def phase(name):
    report["phase"] = name
    save()


def fetch(url, headers=None, data=None, limit=64 * 1024 * 1024):
    request = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(request, timeout=90) as response:
        body = response.read(limit + 1)
    assert len(body) <= limit, "Response exceeds bound"
    return body


def rpc(method, payload):
    return json.loads(fetch("https://app.buildbuddy.io/rpc/BuildBuddyService/" + method,
                            {"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]},
                            json.dumps(payload).encode()))


def blob(file):
    if "contents" in file:
        return base64.b64decode(file["contents"], validate=True)
    uri = file["uri"]
    assert uri.startswith("bytestream://"), "Original artifact is unavailable remotely"
    body = fetch("https://app.buildbuddy.io/file/download?" + urllib.parse.urlencode({
        "invocation_id": INVOCATION, "bytestream_url": uri}),
        {"x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    digest = re.search(r"/blobs/([0-9a-f]{64})/(\d+)$", uri)
    if digest:
        assert hashlib.sha256(body).hexdigest() == digest[1] and len(body) == int(digest[2]), "Original artifact digest mismatch"
    return body


def cas(digest, kind):
    assert re.fullmatch(r"[0-9a-f]{64}", digest["hash"])
    size = int(digest["sizeBytes"])
    assert 0 < size < 64 * 1024 * 1024
    # The official CLI decodes the existing REAPI messages. Its output and
    # errors remain private; only selected values are copied to the verdict.
    result = subprocess.run([str(temp / "bb"), "download", digest["hash"] + "/" + str(size),
                             "--type=" + kind, "--target=grpcs://remote.buildbuddy.io",
                             "--api_key=" + os.environ["BUILDBUDDY_API_KEY"]],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    assert result.returncode == 0, "Original CAS metadata unavailable"
    return json.loads(result.stdout)


def parser_input(root, command):
    # TestRunner passes a runfiles-relative executable to the wrapper. Its
    # TEST_SRCDIR names the actual runfiles tree in the remote execution root.
    env = {v["name"]: v["value"] for v in command.get("environmentVariables", [])}
    relative = Path("tools/nvidia_driver_differ/driver_ast_parser")
    candidates = []
    if "TEST_SRCDIR" in env:
        test_dir = Path(env["TEST_SRCDIR"])
        assert not test_dir.is_absolute() and ".." not in test_dir.parts
        candidates.append(test_dir / "_main" / relative)
        working = Path(command.get("workingDirectory", ""))
        if working.parts:
            candidates.insert(0, working / candidates[0])
        report["runfilesDirectory"] = str(test_dir)
    candidates.append(relative)
    cache = {}

    def directory(digest):
        if digest["hash"] not in cache:
            cache[digest["hash"]] = cas(digest, "Directory")
        return cache[digest["hash"]]

    for candidate in candidates:
        path = candidate
        # A runfile can itself be a relative symlink to the source input.
        for _ in range(16):
            entry = directory(root)
            for n, name in enumerate(path.parts):
                link = next((s for s in entry.get("symlinks", []) if s["name"] == name), None)
                if link is not None:
                    path = Path(os.path.normpath(Path(*path.parts[:n]) / link["target"] / Path(*path.parts[n + 1:])))
                    assert not path.is_absolute() and ".." not in path.parts
                    break
                if n == len(path.parts) - 1:
                    file = next((f for f in entry.get("files", []) if f["name"] == name), None)
                    if file is not None:
                        return str(candidate), file
                child = next((d for d in entry.get("directories", []) if d["name"] == name), None)
                if child is None:
                    report["missingAt"] = str(Path(*path.parts[:n + 1]))
                    break
                entry = directory(child["digest"])
            if link is None:
                break
    # Bazel may map paths in a remote action. Recover the actual named input
    # from that action's Merkle tree if the logical runfiles path was mapped.
    pending = [(Path(), root)]
    visited = set()
    while pending:
        prefix, digest = pending.pop()
        if digest["hash"] in visited:
            continue
        visited.add(digest["hash"])
        assert len(visited) <= 1500, "Original input tree exceeded the metadata bound"
        entry = directory(digest)
        for file in entry.get("files", []):
            if file["name"] == relative.name and prefix.match("**/nvidia_driver_differ"):
                return str(prefix / file["name"]), file
        pending.extend((prefix / d["name"], d["digest"]) for d in entry.get("directories", []))
    report["uniqueInputDirectories"] = len(visited)
    raise AssertionError("Parser absent from original action input tree")


def run():
    prior = subprocess.run(["gh", "api", "repos/tamird/gvisor/actions/artifacts/13067164785/zip"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, check=True).stdout
    with zipfile.ZipFile(io.BytesIO(prior)) as archive:
        original = json.loads(archive.read("result.json"))
    assert original["source"] == SOURCE
    stage = original["stages"]["original-arm64-parser-test"]
    assert stage["invocation"] == INVOCATION and stage["ran"] and stage["bazelExit"] == 0
    assert stage["tests"] == {"expected": 1, "executed": 1, "failed": 0, "skipped": 0, "missing": []}
    report.update(behaviorStatus="PASS", tests=stage["tests"], originalBazelFinishedAt=stage["finishedAt"],
                  originalProducerReceiptAt=original["verifiedAt"], sourceInputs=original["sourceInputs"], elf=original["elf"])
    phase("read-original-test-result")
    targets = rpc("GetTarget", {"invocationId": INVOCATION, "targetLabel": TARGET})
    events = {}
    for group in targets.get("targetGroups", []):
        for target in group.get("targets", []):
            if target.get("metadata", {}).get("label") == TARGET:
                for event in target.get("testResultEvents", []):
                    if event.get("id", {}).get("testResult", {}).get("label") == TARGET:
                        events[json.dumps(event["id"], sort_keys=True)] = event
    assert len(events) == 1, "Expected one original test attempt"
    attempt = next(iter(events.values()))["testResult"]
    info = attempt.get("executionInfo", {})
    assert attempt["status"] in (1, "PASSED") and not attempt.get("cachedLocally", False)
    assert info.get("strategy") == "remote" and not info.get("cachedRemotely", False) and info.get("exitCode", 0) == 0
    files = {f["name"]: f for f in attempt.get("testActionOutput", [])}
    xml, log = blob(files["test.xml"]), blob(files["test.log"])
    cases = ET.fromstring(xml).findall(".//testcase")
    assert len(cases) == 1 and cases[0].get("name") == "TestParser"
    assert not any(cases[0].find(name) is not None for name in ("failure", "error", "skipped"))
    assert "NATIVE_WORKER_ARCH=aarch64" in log.decode().splitlines()
    report["originalArtifacts"] = {"testXmlSha256": hashlib.sha256(xml).hexdigest(), "testLogSha256": hashlib.sha256(log).hexdigest()}
    report["testCase"] = {"name": "TestParser", "status": "PASS", "seconds": cases[0].get("time")}
    report["nativeArchitecture"] = "aarch64"
    start = attempt.get("testAttemptStart")
    if start is None:
        start = datetime.fromtimestamp(int(attempt["testAttemptStartMillisEpoch"]) / 1000, timezone.utc).isoformat()
    duration = float(attempt["testAttemptDuration"].removesuffix("s")) if "testAttemptDuration" in attempt else int(attempt["testAttemptDurationMillis"]) / 1000
    report["testAttempt"] = {"startedAt": start, "seconds": duration,
                             "finishedAt": (datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(seconds=duration)).isoformat(),
                             "strategy": "remote", "cachedLocally": False, "cachedRemotely": False}
    phase("read-original-execution")
    response = rpc("GetExecution", {"executionLookup": {"invocationId": INVOCATION, "targetLabel": TARGET}, "inlineExecuteResponse": True})
    executions = [e for e in response.get("execution", []) if e.get("targetLabel") == TARGET and e.get("actionMnemonic") == "TestRunner"]
    report["matchingExecutionRecords"] = len(executions)
    assert executions, "Original TestRunner execution not retained"
    phase("read-original-action-inputs")
    cli = fetch("https://github.com/buildbuddy-io/bazel/releases/download/5.0.498/bazel-5.0.498-linux-x86_64", limit=90 * 1024 * 1024)
    assert hashlib.sha256(cli).hexdigest() == "205fb4074012858c858ec357cb0cf1ed0b1fbf8707c5f385efd4abd52bb2cead"
    (temp / "bb").write_bytes(cli)
    (temp / "bb").chmod(0o500)
    selected = []
    for execution in executions:
        action = cas(execution["actionDigest"], "Action")
        command = cas(action["commandDigest"], "Command")
        arguments = command.get("arguments", [])
        if "native-arm64" in arguments and any(a.endswith("/driver_ast_parser_test") for a in arguments):
            selected.append((execution, action, command))
    assert len(selected) == 1, "Expected one original native TestRunner command"
    execution, action, command = selected[0]
    result = execution["executeResponse"]
    assert execution.get("stage") in (4, "COMPLETED") and execution.get("status", {}).get("code", 0) == 0
    assert not result.get("cachedResult", False) and result.get("status", {}).get("code", 0) == 0
    assert execution.get("exitCode", 0) == 0 and result["result"].get("exitCode", 0) == 0
    properties = {p["name"]: p["value"] for p in (action.get("platform") or command.get("platform", {})).get("properties", [])}
    assert all(properties.get(k) == v for k, v in PROPERTIES.items()), "Original worker properties differ"
    path, file = parser_input(action["inputRootDigest"], command)
    assert file["digest"]["hash"] == BINARY["hash"] and int(file["digest"]["sizeBytes"]) == int(BINARY["sizeBytes"]), "Original parser input digest differs"
    metadata = result["result"].get("executionMetadata") or execution["executedActionMetadata"]
    worker = metadata.get("worker") or metadata.get("executorId")
    assert worker, "Original worker identity not retained"
    report["worker"] = {"identitySha256": hashlib.sha256(worker.encode()).hexdigest(), "properties": PROPERTIES,
                        "remote": True, "cached": False, "uname": "aarch64"}
    report["action"] = {"digest": execution["actionDigest"], "inputRootDigest": action["inputRootDigest"],
                        "binaryInput": {"path": path, "digest": BINARY}}
    report["executionStartedAt"] = metadata["executionStartTimestamp"]
    report["executionFinishedAt"] = metadata["executionCompletedTimestamp"]
    report.update(status="PASS", phase="complete", verifiedAt=datetime.now(timezone.utc).isoformat())


save()
try:
    run()
except Exception as exc:
    report["failure"] = type(exc).__name__
    if isinstance(exc, urllib.error.HTTPError):
        report["httpStatus"] = exc.code
    if isinstance(exc, (KeyError, AssertionError)):
        # Our literal assertions and field names contain neither credentials nor
        # untrusted diagnostic bodies. Preserve the actual missing-input cause.
        report["reason"] = str(exc)
    report["verifiedAt"] = datetime.now(timezone.utc).isoformat()
finally:
    save()
print(report["status"], report["phase"])
sys.exit(report["status"] != "PASS")
