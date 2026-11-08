#!/usr/bin/python3
"""Read original named Go cases, executable identity and representative child waits."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

BASE = "7d1b049f5d541992b47cc38cf564025908a9ed7b"
FIX = "ee36b19f4c92fe81de475e18cc6b7f546f6aa639"
TREE = "02c7947ef21da2beea0cabb984f882ed9de5b4d0"
WORK = Path(__file__).resolve().parent
PROC = "//pkg/shim/v1/proc:proc_test"
SHIM = "//pkg/shim/v1:shim_test"
FAMILY = {
    PROC: {
        "TestRuntimeCallsAreBounded": ["Status", "Stats", "Kill", "Update"],
        "TestCreateWedgedRunscIsBounded": [], "TestStatsWedgedRunscTimesOut": [],
        "TestStatusWedgedRunscTimesOut": [], "TestKillWedgedRunscTimesOut": [],
        "TestKillAllWedgedRunscReturnsAndSparesSandbox": [],
        "TestExecRuntimeCallsAreBounded": ["Start", "Kill"],
        "TestLongRuntimeOperationsAreBounded": ["Start", "Restore", "Checkpoint"],
    },
    SHIM: {
        "TestManagerStopBoundsWedgedRuntime": ["blocked-cleanup=false", "blocked-cleanup=true"],
        "TestManagerStopPreservesCallerCancellation": ["deadline=false", "deadline=true"],
    },
}
REGRESSION = {
    PROC: {"TestFuseStatusRetryPreservesOutcome": ["Stopped", "Error", "Running"],
           "TestFailedDeleteRetainsOnlyLivePidfd": ["false", "true"],
           "TestPinnedSandboxRemainsOwnedAcrossExec": []},
}
ONE = {PROC: {"TestStatusWedgedRunscTimesOut": []}}
CLI = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g"]
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off", "--remote_cache=",
         "--remote_executor=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
         "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
         "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox"]
RESULT = {"base": BASE, "fix": FIX, "tree": TREE, "carrier": os.environ["GITHUB_SHA"], "cells": {}}


def output(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def save() -> None:
    (WORK / "result.json").write_text(json.dumps(RESULT, indent=2, sort_keys=True) + "\n")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(line: str) -> str:
    return re.sub(r'https?://\S+|/(?:home/runner|tmp)/\S+', '<path>', line)[:320]


def run(name: str, owners: dict[str, dict[str, list[str]]], mode: str, verdict: str) -> None:
    row = RESULT["cells"][name] = {"mode": mode, "verdict": verdict, "verified": False, "tests": {}}
    names = sorted({n for suite in owners.values() for n in suite})
    bep = WORK / (name + ".bep")
    log = WORK / (name + ".log")
    cmd = [*CLI, "test", *FLAGS, "--strategy=TestRunner=local", "--test_sharding_strategy=disabled",
           "--nocache_test_results", "--flaky_test_attempts=1", "--keep_going",
           "--build_event_json_file=" + str(bep), "--run_under=" + str(WORK / "entry.py"),
           "--test_env=SHIM_QUAL_MODE=" + mode, "--test_arg=-test.v",
           "--test_arg=-test.run=^(" + "|".join(names) + ")$", *owners]
    try:
        row["source"] = output("git", "rev-parse", "HEAD")
        with log.open("w") as stream:
            row["bazel_exit"] = subprocess.run(cmd, stdout=stream, stderr=subprocess.STDOUT).returncode
        for line in bep.open():
            event = json.loads(line)
            if "started" in event:
                row["invocation"] = event["started"]["uuid"]
            key = event["id"].get("testResult")
            if key is None or "testResult" not in event:
                continue
            label = key["label"]
            result = event["testResult"]
            info = result.get("executionInfo", {})
            assert label in owners and label not in row["tests"], label
            test = row["tests"][label] = {
                "config": key["configuration"]["id"], "attempt": key.get("attempt", 1),
                "status": result["status"], "strategy": info.get("strategy"),
                "cached": result.get("cachedLocally", False) or info.get("cachedRemotely", False),
            }
            for entry in result["testActionOutput"]:
                if entry["name"] not in {"test.log", "test.xml"}:
                    continue
                uri = urlsplit(entry["uri"])
                assert uri.scheme == "file" and not uri.netloc, uri.scheme
                data = Path(unquote(uri.path)).read_bytes()
                sha = digest(data)
                assert entry.get("digest", sha) == sha and int(entry.get("length", len(data))) == len(data)
                test[entry["name"]] = {"sha256": sha, "bytes": len(data)}
                if entry["name"] == "test.log":
                    lines = data.decode(errors="replace").splitlines()
                    cases = re.findall(r'^\s*--- (PASS|FAIL|SKIP): (\S+) \(', "\n".join(lines), re.M)
                    test["cases"] = {case: status for status, case in cases}
                    assert len(test["cases"]) == len(cases), "duplicate terminal Go case"
                    test["probe"] = [json.loads(s.removeprefix("Q_SHIM ")) for s in lines if s.startswith("Q_SHIM ")]
                    test["diagnostics"] = [clean(s) for s in lines if (".go:" in s and
                                           ("failed" in s or "error" in s or "did not" in s or "executable" in s))][-12:]
        if row["bazel_exit"]:
            row["diagnostics"] = [clean(s) for s in log.read_text(errors="replace").splitlines()
                                  if s.startswith("ERROR:")][:6]
        save()
        assert set(row["tests"]) == set(owners), sorted(row["tests"])
        for label, suite in owners.items():
            test = row["tests"][label]
            assert test["attempt"] == 1 and not test["cached"] and test["strategy"] in {"local","standalone"}
            assert test["test.xml"]["bytes"] > 0 and test["test.log"]["bytes"] > 0
            probe, = test["probe"]
            assert probe["uid"] > 0 and probe["mode"] == mode and probe["sh"] == (mode == "ordinary")
            assert probe["sleep"] == (mode == "ordinary") and re.fullmatch(r"[0-9a-f]{64}", probe["sha256"])
            expected = {case for group, children in suite.items() for case in
                        ([group] + [group + "/" + child for child in children])}
            assert set(test["cases"]) == expected, sorted(expected ^ set(test["cases"]))
            if verdict in {"pass", "original-false-pass", "fixed-trace"}:
                assert all(s == "PASS" for s in test["cases"].values()), test["cases"]
                assert test["status"] == "PASSED" and probe["exit"] == 0 and row["bazel_exit"] == 0
            if verdict == "missing":
                assert test["cases"] == {"TestStatusWedgedRunscTimesOut": "FAIL"}
                assert test["status"] == "FAILED" and probe["exit"] != 0 and row["bazel_exit"] != 0
                assert any("wedged runsc did not start" in s for s in test["diagnostics"])
            if verdict in {"original-false-pass", "fixed-trace"}:
                child, = probe["children"]
                if verdict == "original-false-pass":
                    assert child["program"] == "fake-runsc" and child["exec"] == "-1 ENOENT", child
                else:
                    assert child["program"] == "wedged-runsc" and child["exec"] == "0", child
                    assert child["killed"] and child["waits"], child
        row["verified"] = True
    except Exception as exc:
        row["observer"] = clean(f"{type(exc).__name__}: {exc}")
    save()
    print(name, row["verified"], row.get("observer"), flush=True)


def bind(ref: str) -> None:
    subprocess.run(["git","checkout","--detach",ref],check=True)
    assert output("git","rev-parse","HEAD") == ref


def main() -> bool:
    assert output("git","rev-parse","HEAD") == RESULT["carrier"]
    assert output("git","rev-parse","HEAD^") == FIX and output("git","rev-parse",FIX+"^") == BASE
    assert output("git","rev-parse",FIX+"^{tree}") == TREE
    assert set(output("git","diff","--name-only",FIX,"HEAD").splitlines()) == {
        ".github/qualification/shim_wedge/check.py", ".github/qualification/shim_wedge/entry.py",
        ".github/workflows/build.yml"}
    bind(BASE)
    run("stock-ordinary", FAMILY, "ordinary", "pass")
    run("stock-missing-shell", ONE, "trace", "original-false-pass")
    bind(FIX)
    run("fixed-ordinary-regressions", REGRESSION, "ordinary", "pass")
    run("fixed-masked", FAMILY, "masked", "pass")
    run("fixed-traced", ONE, "trace", "fixed-trace")

    # Prove the corrected original can distinguish a failure to execute the child.
    path = Path("pkg/shim/v1/testutil/wedged.go")
    before = path.read_bytes()
    original = b"os.Symlink(exe, path)"
    assert before.count(original) == 1
    after = before.replace(original, b'os.Symlink(exe+".missing-shim-qualification", path)')
    path.write_bytes(after)
    RESULT["negative_control"] = {"source": FIX, "file": str(path), "before": digest(before), "after": digest(after)}
    run("fixed-missing-helper", ONE, "masked", "missing")
    path.write_bytes(before)
    assert not output("git","diff","--name-only")
    RESULT["verified"] = all(r["verified"] for r in RESULT["cells"].values()) and len(RESULT["cells"]) == 6
    save()
    return RESULT["verified"]


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
