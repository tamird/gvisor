"""Run the original parser test with the exact verified ARM64 compiler inputs."""
import hashlib
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import json
import os
import re
import subprocess
import sys
import time
import struct
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from uuid import uuid4


temp = Path(os.environ["RUNNER_TEMP"])
dest = temp / "compiler-owners"
target = "//tools/nvidia_driver_differ:driver_ast_parser"
flag = "-fno-color-diagnostics"
report = {"status": "INCOMPLETE", "source": os.environ["PRODUCT"], "original": "17eb53a0-0121-42cd-88a4-dc6780e418e1", "stages": {}}
def save():
    (dest / "result.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

def diagnose(path):
    lines = path.read_text(errors="replace").splitlines()
    return [s[:600] for s in lines if not any(k in s.lower() for k in
        ("header=", "key=", "token=", "bearer", "authorization", "https://", "grpc://", "grpcs://"))
        and any(k in s for k in ("ERROR:", "FAILED:", "Resource Exhausted", "Build did NOT",
            "Invalid", "failed to", "parse option", "Elapsed time:", "processes:", "Build completed"))][-25:]

def baz(stage, cmd, arch, flags, args, minutes):
    iid = str(uuid4())
    out, err = (temp / (stage + "." + ext) for ext in ("out", "err"))
    row = report["stages"][stage] = {"invocation": iid, "status": "INCOMPLETE"}; save()
    row["startedAt"] = datetime.now(timezone.utc).isoformat()
    start = time.monotonic()
    with out.open("wb") as stdout, err.open("wb") as stderr:
        ret = subprocess.run(["timeout", "--signal=INT", "--kill-after=25s", f"{minutes}m", "bazelisk",
            "--bazelrc=" + str(temp / "images.bazelrc"), cmd, "--config=" + arch, "--color=no", "--curses=no",
            "--remote_retries=1", "--invocation_id=" + iid, *flags, *args], stdout=stdout, stderr=stderr)
    row.update(finishedAt=datetime.now(timezone.utc).isoformat(), bazelExit=ret.returncode, elapsed=round(time.monotonic()-start, 2), diagnostics=diagnose(err),
               status="PASS" if not ret.returncode else "FAIL")
    save()
    print(stage, row["status"], row["bazelExit"], iid, flush=True)
    return row, out

def run():
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == report["source"]
    # These are the successful ARM64 link's immutable inputs, not a newly
    # reconstructed set of failures from a still-changing older invocation.
    with zipfile.ZipFile(temp / "prior-links.zip") as archive:
        receipt = json.loads(archive.read("result.json"))
    assert receipt["source"] == report["source"], "Link receipt source mismatch"
    linked = receipt["stages"]["linked-aarch64"]
    assert linked["status"] == "PASS" and linked["invocation"] == "17eb53a0-0121-42cd-88a4-dc6780e418e1"
    assert linked["binary"]["sha256"] == "c39e7d7ceff9cc255fcfe35aea6271f64db9a8da2d5f9e595508e87794550d0d"
    report["verifiedBinaries"] = {"aarch64": linked["binary"]}
    report["phase"] = "fetch-qualified-arm64-elf"
    report["tests"] = {"expected": 1, "executed": 0, "failed": 0, "skipped": 0, "missing": ["TestParser"]}
    save()
    binary = Path("tools/nvidia_driver_differ/driver_ast_parser")
    uri = "bytestream://remote.buildbuddy.io/blobs/" + linked["binary"]["sha256"] + "/" + str(linked["binary"]["bytes"])
    url = "https://app.buildbuddy.io/file/download?" + urllib.parse.urlencode({
        "invocation_id": linked["invocation"], "bytestream_url": uri, "filename": "driver_ast_parser"})
    request = urllib.request.Request(url, headers={"x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=120) as response, binary.open("wb") as out:
        while block := response.read(2**20):
            out.write(block)
            digest.update(block)
    report["phase"] = "verify-qualified-arm64-elf"
    assert digest.hexdigest() == linked["binary"]["sha256"], "ELF digest mismatch"
    assert binary.stat().st_size == linked["binary"]["bytes"], "ELF size mismatch"
    with binary.open("rb") as f:
        header = f.read(64)
        assert header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20], "little") == 183, "Not an ARM64 ELF"
        offset = int.from_bytes(header[32:40], "little")
        entry_size, count = struct.unpack_from("<HH", header, 54)
        interpreter = None
        for i in range(count):
            f.seek(offset + i * entry_size)
            segment = f.read(entry_size)
            if int.from_bytes(segment[:4], "little") == 3:
                f.seek(int.from_bytes(segment[8:16], "little"))
                interpreter = f.read(int.from_bytes(segment[32:40], "little")).rstrip(b"\0").decode()
    dynamic = subprocess.check_output(["readelf", "-d", str(binary)], text=True)
    needed = re.findall(r"\(NEEDED\).*\[(.*?)\]", dynamic)
    report["elf"] = {"sha256": digest.hexdigest(), "bytes": binary.stat().st_size,
                     "machine": 183, "interpreter": interpreter, "neededLibraries": needed}
    save()
    binary.chmod(0o555)
    report["phase"] = "bind-original-test"
    owning_build = Path("tools/nvidia_driver_differ/BUILD")
    build = owning_build.read_text()
    start = build.index("cc_binary(")
    end = build.index("\n\ngo_test(", start)
    owning_build.write_text(build[:start] + 'exports_files(["driver_ast_parser"])' + build[end:])
    source_inputs = {}
    for name in ("driver_ast_parser_test.go", "test_struct.cc"):
        p = "tools/nvidia_driver_differ/" + name
        expected = subprocess.check_output(["git", "show", report["source"] + ":" + p])
        assert Path(p).read_bytes() == expected, "Original test source changed"
        source_inputs[p] = hashlib.sha256(expected).hexdigest()
    report["sourceInputs"] = source_inputs
    assert subprocess.check_output(["git", "diff", "--name-only"], text=True).splitlines() == [str(owning_build)], "Unexpected source delta"
    report["buildAdaptation"] = "Replace only cc_binary with exports_files for the exact retained ELF at the original runfiles path"
    report["phase"] = "native-arm64-test"
    selected = ["--extra_execution_platforms=@epoll_gnu//:gnu"]
    save()
    # Compiler platforms already set their own properties. Only the empty
    # ARM64 target platform gets these defaults; the compiled inputs stay the same.
    native_properties = {
        "Arch": "arm64", "OSFamily": "linux", "workload-isolation-type": "oci",
        "container-image": "docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
    }
    native = ["--use_target_platform_for_tests", "--strategy=TestRunner=remote",
              *("--remote_default_exec_properties=" + k + "=" + v for k, v in native_properties.items())]
    row, graph = baz("arm64-test-platform", "aquery", "aarch64", [*selected, *native],
        ["--output=jsonproto", "--noinclude_artifacts", 'mnemonic("TestRunner",' + target + '_test)'], 9)
    assert row["bazelExit"] == 0, "ARM64 test graph failed"
    actions = json.loads(graph.read_bytes()).get("actions", [])
    assert len(actions) == 1 and actions[0]["executionPlatform"].endswith("//tools/bazeldefs:linux_arm64"), "TestRunner is not bound to the ARM64 target platform"
    exec_log = temp / "arm64-test-execution.json"
    bep = temp / "arm64-test-bep.json"
    run_under = '/bin/sh -c \'printf "NATIVE_WORKER_ARCH=%s\\n" "$(uname -m)"; test "$(uname -m)" = aarch64 || exit 125; exec "$@"\' native-arm64'
    row, _ = baz("original-arm64-parser-test", "test", "aarch64", [*selected, *native], [
        "--test_output=errors", "--test_arg=-test.v", "--nocache_test_results",
        "--runs_per_test=1", "--flaky_test_attempts=1", "--run_under=" + run_under,
        "--execution_log_json_file=" + str(exec_log), "--build_event_json_file=" + str(bep),
        target + "_test"], 75)
    logs = Path("bazel-testlogs/tools/nvidia_driver_differ/driver_ast_parser_test")
    for name in ("test.log", "test.xml"):
        if (logs / name).is_file():
            (dest / name).write_bytes((logs / name).read_bytes())
    row["testLines"] = []
    if (dest / "test.log").is_file():
        row["testLines"] = [line.strip() for line in (dest / "test.log").read_text().splitlines()
                            if re.fullmatch(r"NATIVE_WORKER_ARCH=aarch64|=== RUN   TestParser|--- (?:PASS|FAIL|SKIP): TestParser \(.*\)", line.strip())]
    row["tests"] = {"expected": 1, "executed": 0, "failed": 0, "skipped": 0, "missing": ["TestParser"]}
    if (dest / "test.xml").is_file():
        cases = ET.parse(dest / "test.xml").getroot().findall(".//testcase")
        row["tests"].update(executed=len(cases), failed=sum(c.find("failure") is not None or c.find("error") is not None for c in cases),
                            skipped=sum(c.find("skipped") is not None for c in cases),
                            missing=[] if any(c.get("name") == "TestParser" for c in cases) else ["TestParser"])
    report["tests"] = row["tests"]
    row["ran"] = (row["bazelExit"] == 0 and row["tests"] == {"expected": 1, "executed": 1, "failed": 0, "skipped": 0, "missing": []}
                  and "NATIVE_WORKER_ARCH=aarch64" in row["testLines"]
                  and any("--- PASS: TestParser " in s for s in row["testLines"]))
    # Preserve the actual test outcome even if evidence collection fails later.
    report["status"] = "FAIL" if row["tests"]["failed"] else "INCOMPLETE"
    save()
    report["phase"] = "verify-native-test-execution"
    save()
    spawns = []
    if exec_log.is_file():
        text, decoder, pos = exec_log.read_text(), json.JSONDecoder(), 0
        while pos < len(text):
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos == len(text):
                break
            spawn, pos = decoder.raw_decode(text, pos)
            if spawn.get("mnemonic") == "TestRunner" and spawn.get("targetLabel", "").endswith(target + "_test"):
                spawns.append(spawn)
    tests = [s for s in spawns if any(p.endswith("/test.log") for p in s.get("listedOutputs", []))]
    assert len(tests) == 1, f"Expected one test action, got {len(tests)}"
    executed = tests[0]
    properties = {p["name"]: p["value"] for p in executed.get("platform", {}).get("properties", [])}
    row["worker"] = {"uname": "aarch64" if "NATIVE_WORKER_ARCH=aarch64" in row["testLines"] else None,
                     "runner": executed.get("runner"), "cacheHit": executed.get("cacheHit", False), "properties": {k: properties.get(k) for k in native_properties}}
    assert all(properties.get(k) == v for k, v in native_properties.items()), "Unexpected remote worker properties"
    assert executed.get("runner") == "remote" and not executed.get("cacheHit", False), row["worker"]
    linked = report["verifiedBinaries"]["aarch64"]
    inputs = [f for f in executed.get("inputs", []) if f["path"].endswith("/driver_ast_parser")]
    assert inputs and all(f["digest"]["hash"] == linked["sha256"] and int(f["digest"]["sizeBytes"]) == linked["bytes"] for f in inputs), "Test inputs do not match the verified ARM64 parser binary"
    row["binary"] = linked
    report["phase"] = "verify-test-timing"
    save()
    events = [json.loads(line) for line in bep.read_text().splitlines()]
    attempts = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label", "").endswith(target + "_test")]
    row["attempts"] = [{k: a.get(k) for k in ("status", "cachedLocally", "cachedRemotely", "testAttemptStartMillisEpoch", "testAttemptDurationMillis")} for a in attempts]
    assert len(attempts) == 1 and not attempts[0].get("cachedLocally", False), "Expected one uncached test attempt"
    result = attempts[0]
    if "testAttemptStartMillisEpoch" in result and "testAttemptDurationMillis" in result:
        row["executionFinishedAt"] = datetime.fromtimestamp((int(result["testAttemptStartMillisEpoch"]) + int(result["testAttemptDurationMillis"])) / 1000, timezone.utc).isoformat()
    report["status"] = "PASS" if row["ran"] else ("FAIL" if row["tests"]["failed"] else "INCOMPLETE")
    report["verifiedAt"] = datetime.now(timezone.utc).isoformat()
    report["manualArtifactRecovery"] = 0
    report["phase"] = "complete"

    save()

save()
try:
    run()
except Exception as exc:
    report["failure"]=type(exc).__name__
    if isinstance(exc, urllib.error.HTTPError):
        report["httpStatus"] = exc.code
    if isinstance(exc, KeyError) and str(exc.args[0]) in {"sections", "optionName", "optionValue", "commandLineLabel", "digest", "hash", "sizeBytes", "path"}:
        report["missingField"] = exc.args[0]
    report["verifiedAt"]=datetime.now(timezone.utc).isoformat()
    save()
    sys.exit(1)
sys.exit(report["status"]!="PASS")
