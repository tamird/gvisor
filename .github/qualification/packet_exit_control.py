"""Run the same PacketImpact exit-status check against both runner implementations."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

FIX = "3518f04377b87fa7136ea9063d9804e4f7d3be8b"
BASE = "975fb172241c7946a9f67b4f52e0349d9240589a"
RUNNER = "test/packetimpact/runner/main.go"
PKG = Path("test/packetimpact/runner/exitstatus")
OWNER = f"//{PKG}:exitstatus_test"
CASES = {"OrdinaryPass", "UnexpectedPass", "OrdinaryFailure", "ExpectedFailure", "MissingTestbench", "SignaledTestbench"}
REGRESSIONS = {"MissingTestbench", "SignaledTestbench"}


def git(*args):
    return subprocess.check_output(["git", *args])


def section(data):
    start = data.index(b"\tcase err := <-testResult:")
    end = data.index(b"\n\t}\n}", start)
    return data[start:end]


def inspect_result(path, phase, row, dest):
    events = [json.loads(line) for line in path.read_bytes().splitlines() if line]
    invocation, = [e["started"]["uuid"] for e in events if "started" in e]
    assert invocation == row["invocation"]
    event, = [e for e in events if "testResult" in e]
    ident, result = event["id"]["testResult"], event["testResult"]
    cfg = ident["configuration"]["id"]
    assert ident["label"].removeprefix("@@") == OWNER
    assert any(i.get("label", "").removeprefix("@@") == OWNER and i.get("configuration", {}).get("id") == cfg
               for e in events if (i := e["id"].get("targetCompleted")))
    assert all(int(ident.get(k, 1)) == 1 for k in ("attempt", "run", "shard"))
    execution = result["executionInfo"]
    row.update(status=result["status"], strategy=execution.get("strategy"), configuration=cfg,
               cached=bool(result.get("cachedLocally") or execution.get("cachedRemotely")), files={})
    assert row["strategy"] == "remote" and not row["cached"]
    docs = {}
    for item in result["testActionOutput"]:
        name = item["name"]
        if name not in ("test.xml", "test.log"):
            continue
        url = urlsplit(item["uri"])
        assert url.scheme == "file" and not url.netloc
        file = Path(unquote(url.path))
        assert file.samefile(Path("bazel-testlogs") / PKG / "exitstatus_test" / name)
        assert 0 < file.stat().st_size <= 2 << 20
        data = file.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert not item.get("digest") or item["digest"] == digest
        assert int(item.get("length", len(data))) == len(data)
        assert not re.search(rb"(?i)authorization:|x-buildbuddy-api-key|github_pat_|ghp_|PRIVATE KEY", data)
        row["files"][name] = {"bytes": len(data), "sha256": digest}
        (dest / (phase + "-" + name)).write_bytes(data)
        docs[name] = data
    xml = ET.fromstring(docs["test.xml"])
    text = docs["test.log"].decode().replace("\x16", "")
    assert not list(xml.iter("skipped"))
    names = {c.attrib["name"].removeprefix("TestRunnerExit/"): c for c in xml.iter("testcase")}
    assert CASES <= names.keys() <= CASES | {"TestRunnerExit"}
    failed = {name for name in CASES if any(names[name].find(tag) is not None for tag in ("failure", "error"))}
    leaves = re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): TestRunnerExit/(\w+) \(", text)
    assert len(leaves) == len(CASES) and {n for _, n in leaves} == CASES
    assert failed == {n for status, n in leaves if status == "FAIL"}
    row["cases"] = {name: status for status, name in leaves}
    row["failed"] = sorted(failed)
    assert failed == (REGRESSIONS if phase == "baseline" else set())
    if phase == "baseline":
        for name in REGRESSIONS:
            message = "".join(names[name].itertext())
            assert "runner exit = 0" in message
    assert (row["exit"], row["status"]) == ((3, "FAILED") if phase == "baseline" else (0, "PASSED"))
    row["verified"] = True


def main():
    donor, bazel, destination, separator, *flags = sys.argv[1:]
    assert separator == "--" and "test" in flags
    dest = Path(destination).resolve()
    root = Path(os.environ["RUNNER_TEMP"]).resolve() / "packet-family"
    assert dest == root / "control-results"
    dest.mkdir(exist_ok=True, parents=True)
    report = {"verified": False, "fixture": donor, "integration": git("rev-parse", "HEAD").decode().strip(), "cells": {}}
    def save():
        (dest / "result.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    save()
    assert re.fullmatch("[0-9a-f]{40}", donor) and git("rev-parse", donor + "^").decode().strip() == FIX
    assert git("rev-parse", FIX + "^").decode().strip() == BASE
    old = git("show", BASE + ":" + RUNNER)
    fixed = git("show", FIX + ":" + RUNNER)
    old_handler, new_handler = section(old), section(fixed)
    assert old.replace(old_handler, new_handler) == fixed and old_handler != new_handler
    source = Path(RUNNER)
    original = source.read_bytes()
    assert section(original) == new_handler and original.count(new_handler) == 1
    assert not PKG.exists() and not git("diff", "HEAD", "--", RUNNER)
    patterns = root / "exitstatus.targets"
    patterns.write_text(OWNER + "\n")
    PKG.mkdir()
    try:
        for name in ("BUILD", "runner_exit_test.go"):
            path = PKG / name
            path.write_bytes(git("show", f"{donor}:{path}"))
        for phase in ("baseline", "candidate"):
            row = report["cells"][phase] = {"verified": False, "invocation": str(uuid.uuid4())}
            src = original.replace(new_handler, old_handler) if phase == "baseline" else original
            source.write_bytes(src)
            row["runner_sha256"] = hashlib.sha256(src).hexdigest()
            bep, console = root / f"exit-{phase}.bep", root / f"exit-{phase}.console"
            try:
                cmd = [bazel, *flags, "--target_pattern_file=" + str(patterns), "--nocache_test_results",
                       "--flaky_test_attempts=1", "--runs_per_test=1", "--test_sharding_strategy=disabled",
                       "--modify_execution_info=^TestRunner$=+no-remote-cache", "--nobuild_event_json_file_path_conversion",
                       "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v", "--test_filter=^TestRunnerExit$",
                       "--test_timeout=180", "--invocation_id=" + row["invocation"], "--build_event_json_file=" + str(bep)]
                with console.open("wb") as log:
                    row["exit"] = subprocess.run(["timeout", "--kill-after=8", "720", *cmd], stdout=log, stderr=subprocess.STDOUT).returncode
                inspect_result(bep, phase, row, dest)
            except Exception as error:
                row["failure"] = type(error).__name__
                tb = error.__traceback__
                while tb.tb_next:
                    tb = tb.tb_next
                row["line"] = tb.tb_lineno
                if console.exists():
                    text = console.read_text(errors="replace").lower()
                    row["signals"] = [s for s in ("no such target", "no such package", "failed to download", "not visible",
                                      "no registered", "no matching", "permission denied", "build interrupted", "error compiling")
                                      if s in text]
            finally:
                bep.unlink(missing_ok=True)
                console.unlink(missing_ok=True)
                save()
    finally:
        source.write_bytes(original)
        for name in ("BUILD", "runner_exit_test.go"):
            (PKG / name).unlink(missing_ok=True)
        PKG.rmdir()
        patterns.unlink(missing_ok=True)
    assert not git("diff", "HEAD", "--", RUNNER)
    report["verified"] = len(report["cells"]) == 2 and all(r["verified"] for r in report["cells"].values())
    save()
    return report["verified"]


if __name__ == "__main__":
    raise SystemExit(not main())
