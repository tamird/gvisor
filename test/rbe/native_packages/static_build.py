"""Qualify the embedded Linux wrapper before the original scratch Go tests."""

import collections
import datetime
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

SOURCE = "c2f5f79dce24877679cede14b85666b7ca65c6de"
TARGETS = {"//src:bazel", "//tools/test:posix_tw", "//tools/test:tw_test"}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def elf(data):
    if data[:6] != b"\x7fELF\x02\x01":
        raise ValueError("Expected a little-endian ELF64 wrapper")
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", data)
    interpreter, needed = [], []
    for i in range(header[10]):
        kind, _, offset, _, _, size, _, _ = struct.unpack_from("<IIQQQQQQ", data, header[5] + i * header[9])
        if kind == 3:
            interpreter.append(data[offset:offset + size].rstrip(b"\x00").decode())
        if kind == 2:
            for j in range(offset, offset + size, 16):
                tag, value = struct.unpack_from("<qQ", data, j)
                if tag == 1:
                    needed.append(value)
    return {"machine": header[2], "interpreter": interpreter, "needed_offsets": needed,
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def actions(log, tmp):
    counts = collections.Counter()
    heavy = collections.Counter()
    heavy_cached = 0
    wrong_network = 0
    platforms = set()
    projection = 'select(has("spawn")) | .spawn | {m:.mnemonic,r:.runner,c:.cacheHit,s:.status,e:.exitCode,p:(.platform.properties // [])}'
    with subprocess.Popen([str(tmp / "bb"), "print", "--raw", "--compact_execution_log=" + str(log)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        with subprocess.Popen(["jq", "-c", projection], stdin=decoder.stdout,
                              stdout=subprocess.PIPE, text=True) as projected:
            decoder.stdout.close()
            for line in projected.stdout:
                row = json.loads(line)
                runner = row["r"] or "unknown"
                counts[runner] += 1
                counts["failed"] += bool(row["s"] or row["e"])
                if row["m"] in ("CppCompile", "CppLink", "Javac"):
                    heavy[runner] += 1
                    heavy_cached += bool(row["c"])
                props = {x["name"]: x["value"] for x in row["p"]}
                if row["m"] in ("CppCompile", "CppLink", "Javac", "TestRunner"):
                    wrong_network += props.get("dockerNetwork") != "bridge"
                platforms.add(tuple((k, props.get(k)) for k in ("container-image", "Arch", "OSFamily", "dockerNetwork")))
            projected_exit = projected.wait()
        decoder_exit = decoder.wait()
    return {"counts": dict(counts), "wrong_network_actions": wrong_network, "compile_link": dict(heavy), "compile_link_cached_actions": heavy_cached, "platforms": [dict(p) for p in platforms],
            "decoder_exits": [decoder_exit, projected_exit]}


def main():
    tmp = Path(os.environ["RUNNER_TEMP"])
    public = tmp / "native-package-evidence"
    public.mkdir(exist_ok=True)
    private = tmp / "supplier-private"
    private.mkdir(exist_ok=True)
    row = {"source": SOURCE, "status": "INCOMPLETE", "phases": {}}
    destination = public / "supplier.json"
    write(destination, row)
    try:
        if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != SOURCE:
            raise ValueError("Supplier source mismatch")
        log = private / "build.log"
        bep = private / "build.bep"
        compact = private / "actions.compact"
        command = ["bazel", "--bazelrc=" + str(tmp / "supplier.bazelrc"), "test", "--config=buildbuddy",
                   "--host_platform=//qualification_platform:buildbuddy",
                   "--extra_execution_platforms=//qualification_platform:buildbuddy",
                   "--jobs=400", "--embed_label=10.0.0-pre.xml-candidate", "--lockfile_mode=error",
                   "--nocache_test_results", "--build_event_json_file=" + str(bep),
                   "--nobuild_event_json_file_path_conversion", "--execution_log_compact_file=" + str(compact),
                   *sorted(TARGETS)]
        row["execution_platform_sha256"] = hashlib.sha256(Path("qualification_platform/BUILD").read_bytes()).hexdigest()
        started = now()
        with log.open("w") as stream:
            code = subprocess.run(["timeout", "--signal=INT", "--kill-after=30s", "70m", *command],
                                  stdout=stream, stderr=subprocess.STDOUT).returncode
        row["phases"]["build_and_owner"] = {"started_at": started, "execution_finished_at": now(), "native_exit": code}
        row["status"] = "FAIL" if code else "INCOMPLETE"
        if code:
            secret_values = [line.split("x-buildbuddy-api-key=", 1)[1] for line in (tmp / "supplier.bazelrc").read_text().splitlines()
                             if "x-buildbuddy-api-key=" in line]
            errors = []
            for line in log.read_text(errors="replace").splitlines():
                if not re.search(r"ERROR:|error:|FAILED:|^(?:\s*Caused by: )?\s*(?:io\.grpc\.)?StatusRuntimeException:|^\s*(?:INVALID_ARGUMENT|UNAVAILABLE|FAILED_PRECONDITION|PERMISSION_DENIED|NOT_FOUND|RESOURCE_EXHAUSTED|INTERNAL|UNKNOWN|UNAUTHENTICATED|DEADLINE_EXCEEDED):", line) or re.search(r"api.key|remote.header|bes.header|authorization", line, re.I):
                    continue
                for value in secret_values:
                    line = line.replace(value, "[REDACTED]")
                line = re.sub(r"(?:https?|grpcs?|bytestream)://\S+", "[URL]", line)
                line = re.sub(r"\bGR[A-Za-z0-9]+\b", "[GROUP]", line)
                line = re.sub(r"\S+@\S+", "[USERINFO]", line)
                line = re.sub(r"\b(?:[A-Za-z0-9_-]+\.)+[A-Za-z0-9_-]+:\d+\b", "[ENDPOINT]", line)
                line = re.sub(r"\b(?:[A-Za-z0-9_-]+\.)+(?:com|io|net|org|internal|local|dev|cloud|app|build)\b", "[HOST]", line)
                errors.append(line[:800])
            row["diagnostic_lines"] = errors[-12:]
        write(destination, row)
        completed = {}
        if bep.exists():
            for line in bep.read_text().splitlines():
                event = json.loads(line)
                if "started" in event:
                    row["invocation"] = event["started"].get("uuid")
                if "finished" in event:
                    row["original_build_finish"] = event["finished"].get("finishTimeMillis", event["finished"].get("finishTime"))
                label = event.get("id", {}).get("targetCompleted", {}).get("label")
                if label in TARGETS:
                    completed.setdefault(label, []).append(event.get("completed", {}).get("success", False))
        row["completed"] = completed
        write(destination, row)
        if code or set(completed) != TARGETS or any(values != [True] for values in completed.values()):
            raise ValueError("Supplier build or owning test did not complete successfully")
        xml = Path("bazel-testlogs/tools/test/tw_test/test.xml").read_bytes()
        root = ET.fromstring(xml)
        cases = root.findall(".//testcase")
        counts = {"passed": 0, "failed": 0, "skipped": 0}
        for case in cases:
            if case.find("failure") is not None or case.find("error") is not None:
                counts["failed"] += 1
            elif case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed":
                counts["skipped"] += 1
            else:
                counts["passed"] += 1
        row["owner_cases"] = counts
        write(destination, row)
        if not cases or row["owner_cases"]["failed"] or row["owner_cases"]["skipped"]:
            raise ValueError("The wrapper owner has missing, failed or skipped cases")
        (public / "supplier-owner.xml").write_bytes(xml)
        candidate = tmp / "bazel-candidate"
        shutil.copy2("bazel-bin/src/bazel", candidate)
        data = candidate.read_bytes()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            embedded = archive.read("embedded_tools/tools/test/posix_tw")
        row["binary_sha256"] = hashlib.sha256(data).hexdigest()
        row["wrapper"] = elf(embedded)
        write(destination, row)
        if row["wrapper"]["machine"] != 62 or row["wrapper"]["interpreter"] or row["wrapper"]["needed_offsets"]:
            row["status"] = "FAIL"
            raise ValueError("Embedded AMD64 wrapper still has dynamic runtime dependencies")
        row["actions"] = actions(compact, tmp)
        if any(row["actions"]["decoder_exits"]) or row["actions"]["counts"].get("failed") or row["actions"]["wrong_network_actions"]:
            raise ValueError("Supplier execution evidence is incomplete or failed")
        if not row["actions"]["compile_link"] or set(row["actions"]["compile_link"]) - {"remote", "remote cache hit"}:
            raise ValueError("Supplier compilation or linkage ran outside RBE")
        started = now()
        with (private / "glibc.log").open("w") as stream:
            code = subprocess.run(["python3", str(tmp / "glibc_probe.py"), "candidate", str(candidate)],
                                  stdout=stream, stderr=subprocess.STDOUT).returncode
        row["phases"]["glibc_control"] = {"started_at": started, "execution_finished_at": now(), "native_exit": code}
        row["status"] = "FAIL" if code else "INCOMPLETE"
        write(destination, row)
        if code:
            raise ValueError("The established glibc native-wrapper integration control failed")
        row["glibc"] = json.loads((tmp / "native-xml-results/candidate-cases.json").read_text())
        row["source_unchanged"] = not subprocess.check_output(["git", "diff", "HEAD", "--"])
        if not row["source_unchanged"]:
            raise ValueError("Tracked supplier source changed during qualification")
        row["status"] = "PASS"
    except Exception as error:
        row["collection_error"] = type(error).__name__ + ": " + str(error)[:500]
    row["verified_at"] = now()
    write(destination, row)
    print(json.dumps({"status": row["status"], "phases": row["phases"]}))
    return int(row["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
