"""Project the original sidecar compiler diagnostic and the replacement test result."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from xml.etree import ElementTree

SOURCE = "ce1bb4b5f1753035f3c1cfe1f0d89cab0f1a6854"
COMPARE = "328e9e71-b7d1-4089-96f9-bcf1ec6bb9da"
STDERR = "ba9a448a8a1b04dcdfbb82fd11db617388a58dfe7b6399eee4f0474992e6ba2c"
WORK = Path(os.environ["RUNNER_TEMP"]) / "source-cc-sidecar"
PUBLIC = "//runsc/fdparking:fdparking_binary"
BINARY = "//runsc/fdparking:_linked/runsc-fd-parking"
TEST = "//runsc/fdparking:fdparking_test"
CASES = {"TestParking", "TestParkingBinaryLayout", "TestParkingMemoryFootprint"}


def compiler_errors(text):
    headers = re.findall(r"^#include\s+<([^>]+)>", Path("runsc/fdparking/fdparking.c").read_text(), re.M)
    failed = [h for h in headers if re.search(r"fatal error:\s*['\x22<]?" + re.escape(h) + r"['\x22>]?[: ]+(?:No such file or directory|file not found)", text, re.I)]
    tokens = ["unable to find library -l" + s for s in ["pthread", "dl", "c", "m", "unwind"]]
    tokens += ["cannot find -l" + s for s in ["pthread", "dl", "c", "m", "unwind"]]
    tokens += ["undefined symbol:", "undeclared inclusion(s)", "cannot find crt", "Permission denied", "Operation not permitted"]
    return {"headers": sorted(set(failed)), "signals": [s for s in tokens if s in text]}


def baseline():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None

    url = f"https://remote.buildbuddy.io/file/download?invocation_id={COMPARE}&bytestream_url=bytestream%3A%2F%2Fremote.buildbuddy.io%2Fblobs%2F{STDERR}%2F195"
    request = urllib.request.Request(url, headers={"x-buildbuddy-api-key": os.environ.pop("BUILDBUDDY_API_KEY")})
    record = {"invocation": COMPARE, "sha256": STDERR, "size": 195, "verified": False}
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=25) as response:
            data = response.read(196)
        if len(data) == 195 and hashlib.sha256(data).hexdigest() == STDERR:
            record.update(verified=True, **compiler_errors(data.decode(errors="replace")))
    except urllib.error.HTTPError as e:
        record["http_status"] = int(e.code)
    except (OSError, TimeoutError):
        record["read_failed"] = True
    (WORK / "baseline.json").write_text(json.dumps(record) + "\n")


def digest(entry):
    match = re.search(r"/blobs/([0-9a-f]{64})/([0-9]{1,12})$", entry.get("uri", ""))
    if match:
        return {"sha256": match[1], "size": int(match[2])}


def collect():
    result = {"source": SOURCE, "targets": {}, "actions": [], "tests": [], "cases": {}, "failures": []}
    result["exit"] = int((WORK / "exit").read_text())
    with (WORK / "events.jsonl").open() as events:
        for line in events:
            event = json.loads(line)
            key = event.get("id", {})
            if "started" in event:
                result["invocation"] = str(uuid.UUID(event["started"]["uuid"]))
            if "finished" in event:
                result["bazel_exit"] = int(event["finished"].get("exitCode", {}).get("code", 0))
            if "targetCompleted" in key:
                target = key["targetCompleted"]
                if target["label"] in (PUBLIC, TEST) and not target.get("aspect"):
                    result["targets"][target["label"]] = bool(event.get("completed", {}).get("success"))
            if "action" in event:
                info = event["action"]
                owner = key["actionCompleted"].get("label")
                mnemonic = info.get("type", "")
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,60}", mnemonic):
                    mnemonic = "other"
                action = {"owner": owner if owner in (PUBLIC, TEST, BINARY) else "dependency",
                          "mnemonic": mnemonic, "success": bool(info.get("success")),
                          "exit": int(info.get("exitCode", 0)), "stderr": digest(info.get("stderr", {}))}
                if owner in (BINARY, PUBLIC) and mnemonic in ("CppCompile", "CppLink", "ExecutableSymlink", "Symlink"):
                    result["actions"].append(action)
                if not action["success"] and len(result["failures"]) < 20:
                    result["failures"].append(action)
            if "testResult" in event:
                assert key["testResult"]["label"] == TEST
                info = event["testResult"]
                strategy = info.get("executionInfo", {}).get("strategy", "")
                result["tests"].append({"passed": info["status"] == "PASSED", "remote": strategy == "remote",
                                        "cached": bool(info.get("cachedLocally")) or bool(info.get("executionInfo", {}).get("cachedRemotely")) or "cache" in strategy.lower(),
                                        "log": next((digest(v) for v in info.get("testActionOutput", []) if v.get("name") == "test.log"), None)})
    testdir = Path("bazel-testlogs/runsc/fdparking/fdparking_test")
    xml = testdir / "test.xml"
    if xml.is_file():
        for row in ElementTree.parse(xml).iter("testcase"):
            name = row.get("name", "")
            if name in CASES:
                result["cases"][name] = "fail" if row.find("failure") is not None or row.find("error") is not None else "skip" if row.find("skipped") is not None or row.get("status") == "notrun" else "pass"
    testlog = testdir / "test.log"
    if testlog.is_file():
        for status, name in re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): (Test\w+) \(", testlog.read_text(errors="replace")):
            if name in CASES:
                result["cases"][name] = status.lower()
    outfile = Path("bazel-bin/runsc/fdparking/runsc-fd-parking")
    if outfile.is_file():
        data = outfile.read_bytes()
        assert data[:6] == b"\x7fELF\x02\x01"
        kind, machine = struct.unpack_from("<HH", data, 16)
        offset = struct.unpack_from("<Q", data, 32)[0]
        entsize, count = struct.unpack_from("<HH", data, 54)
        assert entsize >= 56 and offset + entsize * count <= len(data)
        segments = [struct.unpack_from("<II", data, offset + i * entsize) for i in range(count)]
        result["elf"] = {"machine": machine, "type": kind, "interpreter": any(t == 3 for t, _ in segments),
                         "dynamic": any(t == 2 for t, _ in segments), "loads": [flags for t, flags in segments if t == 1],
                         "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "executable": os.access(outfile, os.X_OK)}
    result["diagnostics"] = compiler_errors((WORK / "console").read_text(errors="replace"))
    (WORK / "sidecar.json").write_text(json.dumps(result, indent=2) + "\n")


assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
if sys.argv[1] == "baseline":
    baseline()
elif sys.argv[1] == "collect":
    collect()
else:
    raise ValueError(sys.argv[1])
