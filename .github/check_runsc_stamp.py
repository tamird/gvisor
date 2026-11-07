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
SOURCE = "d81096ecce6877b6dcb4a00686e68198752153aa"
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
            result["wrong_stamp"] = re.findall(r'runsc --version printed ("[^"\n]+"), want ("[^"\n]+")', text)
            result["missing_stamp"] = re.findall(r'STABLE_VERSION is missing, runsc printed ("[^"\n]+")', text)
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
    fallback = WORK / "exported-status"
    fallback.write_text("#!/bin/sh\nPATH=/no-git exec tools/workspace_status.sh\n")
    downstream = WORK / "downstream-status"
    downstream.write_text("#!/bin/sh\nprintf 'STABLE_VERSION downstream-release\\n'\n")
    fallback.chmod(0o755)
    downstream.chmod(0o755)
    for label, head, tag in (
        ("old-wrong", BASE, "buildkite-test-branch"),
        ("ordinary", SOURCE, None),
        ("buildkite", SOURCE, "buildkite-test-branch"),
        ("dirty-buildkite", SOURCE, "buildkite-test-branch"),
        ("alphabetic", SOURCE, "development"),
        ("release", SOURCE, "release-20990101.0"),
        ("staging", SOURCE, "release-20990101.0-staging"),
        ("without-git", SOURCE, None),
        ("downstream-status", SOURCE, None),
        ("wrong", SOURCE, "buildkite-test-branch"),
        ("unstamped", SOURCE, None),
    ):
        subprocess.run(["git", "switch", "--quiet", "--detach", head], check=True)
        if tag:
            subprocess.run(["git", "tag", tag, head], check=True)
        try:
            if label in ("old-wrong", "wrong"):
                path = Path("runsc/BUILD")
                declaration = '"gvisor.dev/gvisor/runsc/version.version": "{STABLE_VERSION}"'
                text = path.read_text()
                assert declaration in text.split('name = "runsc-plugin-stack"')[0]
                path.write_text(text.replace(declaration, '"gvisor.dev/gvisor/runsc/version.version": "development"', 1))
            if label == "dirty-buildkite":
                with Path("README.md").open("a") as f:
                    f.write("\n")
            status = {"without-git":fallback, "downstream-status":downstream}.get(label, Path("tools/workspace_status.sh"))
            flags = ["--workspace_status_command=" + str(status)] if status in (fallback, downstream) else []
            if label == "unstamped":
                flags.append("--nostamp")
            status_line = subprocess.check_output([str(status)], text=True).rstrip("\n")
            assert status_line.startswith("STABLE_VERSION ")
            expected = status_line.removeprefix("STABLE_VERSION ")
            if label in ("old-wrong", "wrong", "dirty-buildkite"):
                predicted = "buildkite-test-branch-dirty"
            elif label == "without-git":
                predicted = "0.0.0"
            elif label == "downstream-status":
                predicted = "downstream-release"
            elif tag and label != "staging":
                predicted = tag
            else:
                predicted = head[:12]
            row = report["cases"][label] = {"head":head,"stable_status":expected,"producer_matches":expected==predicted}
            save()
            row["test"] = t = test(label, flags)
            save()
            row["binary"] = b = binary(label, flags)
            value = b.get("runsc")
            observation = ('"runsc version ' + str(value) + '"')
            if label == "old-wrong":
                ok = t.get("old_pass") and value == "development" and value != expected
            elif label == "wrong":
                ok = [observation, '"runsc version ' + expected + '"'] in list(map(list,t.get("wrong_stamp",[]))) and value=="development" and value!=expected
            elif label == "unstamped":
                ok = observation in t.get("missing_stamp",[]) and value=="VERSION_MISSING"
            else:
                ok = value == expected
            outcome = "FAILED" if label in ("wrong","unstamped") else "PASSED"
            row["verified"] = bool(row["producer_matches"] and ok and (head==BASE or t.get("go_case"))
                and b.get("machine")==machine and not b.get("exit") and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+].+)?",b.get("spec",""))
                and t["status"]==[outcome] and not t.get("cached",True) and (t["exit"]==0)==(outcome=="PASSED"))
            save()
        finally:
            if label in ("old-wrong","wrong"):
                subprocess.run(["git","restore","--","runsc/BUILD"],check=True)
            if label=="dirty-buildkite":
                subprocess.run(["git","restore","--","README.md"],check=True)
            if tag:
                subprocess.run(["git","tag","-d",tag],check=True)
    rows=report["cases"]
    a,b=rows["old-wrong"]["binary"],rows["wrong"]["binary"]
    report["identical_wrong_ELF"]=bool(a.get("sha256")) and a["sha256"]==b.get("sha256")
    report["verified"]=all(row["verified"] for row in rows.values())
    save()
    assert report["verified"]


if __name__ == "__main__":
    main()
