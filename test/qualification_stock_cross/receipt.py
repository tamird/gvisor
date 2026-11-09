import hashlib
import json
import os
import platform
import re
import shutil
import struct
from pathlib import Path
from xml.etree import ElementTree

from image_execution import execution_records


work = Path(os.environ["RUNNER_TEMP"]) / "stock-cross"
owner = "//test/util:posix_error_test"
wrapper = "//test/qualification_stock_cross:native"
report = {
    "source": "975fb172241c7946a9f67b4f52e0349d9240589a",
    "client": platform.machine(),
    "hostCrossGcc": shutil.which("aarch64-linux-gnu-gcc"),
    "compile": [],
    "link": [],
    "test": [],
}
for phase in ("build", "test"):
    report[phase + "Exit"] = int((work / (phase + ".exit")).read_text())
    bep = work / (phase + ".bep")
    if bep.exists():
        for line in bep.open():
            event = json.loads(line)
            if "started" in event and "uuid" in event["started"]:
                report[phase + "Invocation"] = event["started"]["uuid"]
    spawns = work / (phase + ".spawn")
    if not spawns.exists():
        continue
    with spawns.open(newline="") as stream:
        for _, action in execution_records(stream):
            if not action:
                continue
            label, kind = action.get("targetLabel"), action.get("mnemonic")
            argv = action.get("commandArgs", [])
            if phase == "build" and label == owner and kind in ("CppCompile", "CppLink"):
                key = "compile" if kind == "CppCompile" else "link"
            elif phase == "test" and label == wrapper and kind == "TestRunner" and any(
                x.endswith("/test.cache_status") for x in action.get("listedOutputs", [])
            ):
                key = "test"
            else:
                continue
            properties = {p["name"]: p["value"] for p in action.get("platform", {}).get("properties", [])}
            report[key].append({
                "runner": action.get("runner"),
                "exit": action.get("exitCode", 0),
                "cached": action.get("cacheHit", False),
                "arch": properties.get("Arch"),
                "program": argv[0] if argv else None,
                "outputs": action.get("listedOutputs", []),
            })

binary = Path("bazel-bin/test/util/posix_error_test")
if binary.is_file():
    raw = binary.read_bytes()
    if raw.startswith(b"\x7fELF"):
        report["machine"] = struct.unpack_from("<H", raw, 18)[0]
        report["binarySHA256"] = hashlib.sha256(raw).hexdigest()
testlogs = Path("bazel-testlogs/test/qualification_stock_cross/native")
if (testlogs / "test.log").is_file():
    log = (testlogs / "test.log").read_text(errors="replace")
    report["nativeDiagnostics"] = [line[:340] for line in log.splitlines() if any(
        message in line for message in ("No such file", "error while loading shared libraries", "Exec format error"))][:3]
    report["armObservedBinary"] = ("Native worker: aarch64" in log and "ELF machine: 183" in log
                                   and report.get("binarySHA256", "missing") in log)
if (testlogs / "test.xml").is_file():
    tests = list(ElementTree.parse(testlogs / "test.xml").iter("testcase"))
    report["passed"] = sorted(t.get("name", "") for t in tests if not any(
        t.find(tag) is not None for tag in ("failure", "error", "skipped")))
control = work / "control.json"
if control.is_file() and control.stat().st_size:
    query = json.loads(control.read_text())
    report["defaultPrograms"] = sorted({a["arguments"][0] for a in query.get("actions", []) if a.get("arguments")})
for phase in ("control", "build", "test"):
    log_path = work / (phase + ".log")
    if not log_path.exists():
        continue
    lines = []
    for line in log_path.read_text(errors="replace").splitlines():
        if line.startswith("ERROR:") or re.search(r"fatal error:|undefined reference|error while loading shared libraries|cannot execute binary", line):
            if not re.search(r"authorization|credential|x-buildbuddy-api-key|gh[pousr]_|https?://\S+@", line, re.I):
                lines.append(line[:450])
    report[phase + "Errors"] = lines[:6]

def fresh(actions, runners):
    return bool(actions) and all(not a["cached"] and a["exit"] == 0 and a["runner"] in runners for a in actions)

report["verified"] = (
    report["buildExit"] == report["testExit"] == 0
    and report["client"] == "x86_64" and report.get("machine") == 183
    and fresh(report["compile"], {"local", "linux-sandbox"})
    and fresh(report["link"], {"local", "linux-sandbox"})
    and all("llvm" in a["program"] and "external/" in a["program"] for a in report["compile"] + report["link"])
    and fresh(report["test"], {"remote"}) and all(a["arch"] == "arm64" for a in report["test"])
    and (work / "built.sha256").read_text().split()[0] == report.get("binarySHA256")
    and report.get("armObservedBinary") and report.get("passed") == ["PosixError", "PosixErrorOrNullptr", "PosixErrorOrPosixError"]
)
(work / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
