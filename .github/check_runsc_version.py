"""Check the real runsc output and the test verdict for each stamp."""
from pathlib import Path
from urllib.parse import unquote, urlparse
import hashlib
import json
import os
import platform
import re
import struct
import subprocess

BASE = "70008149cff7b8d3b858877ae9c359b9046d19c5"
SOURCE = "14590f5fbf933f155a44a13d4c606de8932521df"
WORK = Path(os.environ["RUNNER_TEMP"]) / "runsc-version"
CPU = platform.machine()
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc"]
OPTIONS = ["--config=" + CPU, "--jobs=4", "--color=no", "--curses=no", "--noshow_progress"]
TEST = "//runsc:version_test"
BIN = "//runsc:runsc"
report = {"source": SOURCE, "baseline": BASE, "host": CPU, "cases": {}}


def save():
    (WORK / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def events(path):
    if path.exists():
        with path.open() as f:
            for line in f:
                yield json.loads(line)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def source_diagnostics(path):
    result = []
    for raw in path.open(errors="replace"):
        line = raw.strip()
        if not (line.startswith(("ERROR:", "FATAL:", "Caused by:", "FAIL:"))
                or "undefined:" in line or "declared and not used" in line):
            continue
        line = re.sub(r"/(?:home|tmp|opt|usr)/[^\s:]+", "<path>", line)
        line = re.sub(r"https?://\S+", "<url>", line)[:360]
        if line not in result:
            result.append(line)
        if len(result) == 5:
            break
    return result


def test(case, flags):
    bep, log = WORK / (case + ".test.bep"), WORK / (case + ".test.log")
    args = [*BAZEL, "test", *OPTIONS, *flags, "--nocache_test_results", "--test_arg=-test.v",
            "--build_event_json_file=" + str(bep), TEST]
    with log.open("w") as f:
        code = subprocess.run(args, stdout=f, stderr=f, timeout=1900).returncode
    result = {"exit": code, "status": [], "diagnostics": source_diagnostics(log)}
    for event in events(bep):
        if event["id"].get("testResult", {}).get("label") != TEST or "testResult" not in event:
            continue
        tr = event["testResult"]
        result["status"].append(tr["status"])
        result["cached"] = bool(tr.get("cachedLocally") or tr.get("executionInfo", {}).get("cachedRemotely"))
        for item in tr.get("testActionOutput", []):
            if item.get("name") != "test.log":
                continue
            uri = urlparse(item["uri"])
            if uri.scheme != "file":
                continue
            data = Path(unquote(uri.path)).read_bytes()
            text = data.decode(errors="replace")
            result["log_sha256"] = sha(data)
            result["go_case"] = bool(re.search(r"^--- (?:PASS|FAIL): TestVersion(?: |$)", text, re.M))
            result["old_pass"] = "PASS: Got OK version" in text
            result["version_error"] = re.findall(r'unexpected runsc version line ("[^"\n]{0,160}")', text)
    return result


def binary(case, flags):
    bep, log = WORK / (case + ".bin.bep"), WORK / (case + ".bin.log")
    args = [*BAZEL, "run", *OPTIONS, *flags, "--build_event_json_file=" + str(bep), BIN, "--", "--version"]
    with log.open("w") as f:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=f, text=True, timeout=550)
    result = {"exit": p.returncode, "diagnostics": source_diagnostics(log)}
    lines = p.stdout.splitlines()
    if p.returncode == 0 and len(lines) == 2 and lines[0].startswith("runsc version ") and lines[1].startswith("spec: "):
        result["runsc"] = lines[0].removeprefix("runsc version ")[:140]
        result["spec"] = lines[1].removeprefix("spec: ")[:64]
    files = set()
    for ev in events(bep):
        for item in ev.get("namedSetOfFiles", {}).get("files", []):
            if Path(item.get("name", "")).name == "runsc":
                uri = urlparse(item.get("uri", ""))
                if uri.scheme == "file":
                    files.add(Path(unquote(uri.path)))
    if len(files) == 1:
        data = files.pop().read_bytes()
        if data[:6] == b"\x7fELF\x02\x01":
            result["machine"] = struct.unpack_from("<H", data, 18)[0]
            result["sha256"] = sha(data)
    return result


def main():
    machine = {"x86_64": 62, "aarch64": 183}[CPU]
    for stamp in ("development", "VERSION_7"):
        path = WORK / stamp
        path.write_text("#!/bin/sh\nprintf '%s\\n' 'STABLE_VERSION " + stamp + "'\n")
        path.chmod(0o755)
    for label, head, stamp, expectation in (
        ("old-development", BASE, "development", "PASSED"),
        ("fixed-stamped", SOURCE, "default", "PASSED"),
        ("fixed-development", SOURCE, "development", "FAILED"),
        ("fixed-placeholder", SOURCE, "VERSION_7", "FAILED"),
        ("fixed-unstamped", SOURCE, "disabled", "FAILED"),
    ):
        subprocess.run(["git", "switch", "--quiet", "--detach", head], check=True)
        flags = (["--nostamp"] if stamp == "disabled" else [] if stamp == "default"
                 else ["--workspace_status_command=" + str(WORK / stamp)])
        row = report["cases"][label] = {"head": head, "stamp": stamp}
        save()
        row["test"] = t = test(label, flags)
        save()
        row["binary"] = b = binary(label, flags)
        value = b.get("runsc")
        real = subprocess.check_output(["git", "describe", "--always", "--tags", "--exclude",
                                        "release-*-staging", "--abbrev=12", "--dirty"], text=True).strip()
        stamp_matches = (value == real if stamp == "default" else
                         value is not None and ("VERSION" in value or not re.search("[0-9]", value))
                         if stamp == "disabled" else value == stamp)
        marker = (t.get("old_pass") if head == BASE else
                  t.get("go_case") and (expectation == "PASSED" or
                    ('"runsc version ' + str(value) + '"') in t.get("version_error", [])))
        row["verified"] = bool(b.get("machine") == machine and b.get("exit") == 0
                               and stamp_matches and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+].+)?", b.get("spec", ""))
                               and t["status"] == [expectation] and not t.get("cached", True)
                               and bool(t["exit"] == 0) == (expectation == "PASSED") and marker)
        save()
    rows = report["cases"]
    old, new = rows["old-development"]["binary"], rows["fixed-development"]["binary"]
    report["same_malformed_output"] = (old.get("runsc"), old.get("spec")) == (new.get("runsc"), new.get("spec"))
    report["identical_malformed_ELF"] = old.get("sha256") == new.get("sha256") and bool(old.get("sha256"))
    report["verified"] = all(r["verified"] for r in rows.values()) and report["same_malformed_output"]
    save()
    assert report["verified"]


if __name__ == "__main__":
    main()
