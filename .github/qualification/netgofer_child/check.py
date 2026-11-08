#!/usr/bin/python3
"""Verify the original two shutdown cases and the child their reaper actually collected."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

BASE = "975fb172241c7946a9f67b4f52e0349d9240589a"
PRODUCT = "a412fc982b122590975cf4f1257fa52294bc0751"
TREE = "47c5af6780b21bfde3e9af8f0d10e7813c0eac18"
OWNER = "//runsc/sandbox:sandbox_test"
GRACE = "TestStopNetgoferChildExitsWithinGracePeriod"
FORCE = "TestStopNetgoferChildKilledAfterGracePeriod"
WORK = Path(__file__).resolve().parent
OUT = WORK / "receipt"
REPORT = {"carrier": os.environ["GITHUB_SHA"], "baseline": BASE, "product": PRODUCT,
          "product_tree": TREE, "owner": OWNER,
          "stock_artifact": 13607151542, "stock_zip_sha256": "5638657462a844984a01850bb9cac32b397497ef929ecd751d44c37dc3c59ed9", "cells": {}}
CLI = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g"]
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off", "--remote_cache=",
         "--remote_executor=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
         "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
         "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
         "--strategy=TestRunner=local", "--test_sharding_strategy=disabled",
         "--nocache_test_results", "--flaky_test_attempts=1"]


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def save() -> None:
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def collect(row: dict, arm: str, cases: list[str], bep: Path, pattern: str) -> None:
    events = [json.loads(line) for line in bep.open()]
    row["invocation"], = [e["started"]["uuid"] for e in events if "started" in e]
    e, = [e for e in events if "testResult" in e and "testResult" in e["id"]]
    key, result = e["id"]["testResult"], e["testResult"]
    info = result.get("executionInfo", {})
    assert key["label"] == OWNER and key.get("attempt", 1) == 1 and key.get("run", 1) == 1
    row.update(config=key["configuration"]["id"], status=result["status"], strategy=info.get("strategy"),
               cached=result.get("cachedLocally", False) or info.get("cachedRemotely", False))
    assert not row["cached"] and row["strategy"] == "local", row["strategy"]
    files = row["originals"] = {}
    for f in result["testActionOutput"]:
        name = f["name"]
        if name not in {"test.log", "test.xml"}:
            continue
        uri = urlsplit(f["uri"])
        assert uri.scheme == "file" and not uri.netloc and name not in files
        data = Path(unquote(uri.path)).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        assert 0 < len(data) < 65536 and f.get("digest", sha) == sha and int(f.get("length", len(data))) == len(data)
        files[name] = {"bytes": len(data), "sha256": sha}
        (OUT / f"{arm}.{name}").write_bytes(data)
    assert set(files) == {"test.log", "test.xml"}
    ET.parse(OUT / f"{arm}.test.xml")
    log = (OUT / f"{arm}.test.log").read_text()
    marker, = [json.loads(s.removeprefix("Q_NETGOFER ")) for s in log.splitlines() if s.startswith("Q_NETGOFER ")]
    row["probe"] = marker
    assert marker["uid"] > 0 and set(marker["missing"]) == {"/bin/cat", "/bin/sleep", "/usr/bin/cat", "/usr/bin/sleep"}
    assert "-test.run=" + pattern in marker["argv"] and "-test.v" in marker["argv"]
    assert re.fullmatch("[0-9a-f]{64}", marker["elf_sha256"])
    terminal = re.findall(r"^--- (PASS|FAIL|SKIP): (Test\w+) \(", log, re.M)
    row["named_results"] = terminal
    assert sorted(terminal) == sorted(("PASS", c) for c in cases)
    started = re.findall(r"^=== RUN\s+(Test\w+)$", log, re.M)
    assert sorted(started) == sorted(cases)
    assert row["status"] == "PASSED" and row["bazel_exit"] == 0 and marker["exit"] == 0
    child, = marker["children"]
    assert (child["path"], child["argv0"], child["exec"]) == ("/proc/self/exe", "fake-netgofer", "0")
    pid = child["pid"]
    calls = child["waits"]
    forced = arm == "forced"
    reason = "CLD_KILLED" if forced else "CLD_EXITED"
    status = "SIGKILL" if forced else "0"
    assert any(s.startswith("waitid(") and "WNOWAIT" in s and reason in s and "si_status=" + status in s for s in calls), calls
    assert any(s.startswith(f"wait4({pid},") and ("WTERMSIG(s) == SIGKILL" if forced else "WEXITSTATUS(s) == 0") in s for s in calls), calls
    assert any(re.match(rf"kill\({pid}, SIGKILL\)\s+= 0", s) for s in calls) == forced, calls
    assert any(re.match(rf"kill\({pid}, 0\)\s+= -1 ESRCH", s) for s in calls), calls


def run(arm: str, revision: str, cases: list[str]) -> None:
    row = REPORT["cells"][arm] = {"source": revision, "tests": cases, "verified": False}
    bep, log = WORK / (arm + ".bep"), WORK / (arm + ".bazel-log")
    try:
        git("checkout", "--detach", revision)
        assert git("rev-parse", "HEAD") == revision and not git("diff", "--name-only")
        row["source_blob"] = git("rev-parse", "HEAD:runsc/sandbox/network_test.go")
        pattern = "^(" + "|".join(cases) + ")$"
        with log.open("w") as stream:
            row["bazel_exit"] = subprocess.run([*CLI, "test", *FLAGS,
                "--build_event_json_file=" + str(bep), "--run_under=" + str(WORK / "entry.py"),
                "--test_arg=-test.v", "--test_arg=-test.run=" + pattern, OWNER],
                stdout=stream, stderr=subprocess.STDOUT).returncode
        collect(row, arm, cases, bep, pattern)
        row["verified"] = True
    except Exception as err:
        row["error"] = type(err).__name__ + " " + str(err)[:600]
        if log.exists():
            lines = log.read_text(errors="replace").splitlines()
            row["public_downloads"] = sorted({url[:300] for line in lines if "Error downloading" in line
                for url in re.findall(r"https://github[.]com/[\w.-]+/[\w.-]+/(?:releases/download|archive)/[\w./+-]+", line)})[:6]
            row["client"] = [re.sub(r"https?://\S+|/(?:tmp|home/runner)/\S+", "<path>", s)[:300]
                             for s in lines if "ERROR:" in s][:6]
    finally:
        save()


def main() -> bool:
    save()
    assert git("rev-parse", "HEAD") == REPORT["carrier"] and git("rev-parse", "HEAD^") == PRODUCT
    assert git("rev-parse", PRODUCT + "^") == BASE and git("rev-parse", PRODUCT + "^{tree}") == TREE
    assert git("diff", "--name-only", BASE, PRODUCT).splitlines() == [
        "runsc/sandbox/network.go", "runsc/sandbox/network_test.go", "runsc/sandbox/sandbox.go"]
    prior = "a002f475d31483583d61e63381d6919100d1be30^^"
    assert not git("diff", "--name-only", prior, BASE, "--", "MODULE.bazel", "MODULE.bazel.lock",
                   ".bazelrc", ".bazelversion", "runsc/sandbox/BUILD", "runsc/sandbox/network.go",
                   "runsc/sandbox/network_test.go", "runsc/sandbox/sandbox.go")
    run("graceful", PRODUCT, [GRACE])
    run("forced", PRODUCT, [FORCE])
    cells = REPORT["cells"]
    REPORT["verified"] = (all(row["verified"] for row in cells.values()) and
        cells["graceful"]["probe"]["elf_sha256"] == cells["forced"]["probe"]["elf_sha256"])
    save()
    return REPORT["verified"]


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
