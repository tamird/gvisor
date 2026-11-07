"""Qualify the standalone statefile reader in the declared-guest integration."""

import json
import os
from pathlib import Path
import platform
import re
import runpy
import subprocess
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

WORK = Path(os.environ["RUNNER_TEMP"]) / "cmd-guest-fixture"
RUNNER = WORK / "cmd_guest.py"
H = runpy.run_path(str(RUNNER))
CPU = platform.machine()
PRODUCT = "91d9996ab5685836c24c86ffe1d3bbce508c1365"
SOURCE = "e34a3859cd0913d12f1952a2f51bc8e23aac3907"
OWNER = H["OWNER"]
CHECKPOINT = H["CHECKPOINT"]
TARGETS = {
    "//runsc/cmd/sentry/forwardcmd:forwardcmd_test":
        ("TestStatefileDumpMemoryFiles", ["inline", "split", "legacy", "generic", "missing-kernel"]),
    "//pkg/sentry/pgalloc:pgalloc_test":
        ("TestSaveToPreservesContents", ["memfd", "memfd_excluding_committed_zero_pages", "disk-backed"]),
    "//pkg/state/pretty:pretty_test": ("TestPrintStreamNonObjectPropagatesReadError", []),
}


def run(name, owners, pattern, result):
    bep = WORK / (name + ".bep")
    args = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3000m",
            "test", "--config=" + CPU, "-c", "opt", "--strip=never", "--jobs=3", "--local_resources=memory=9900",
            "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
            "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
            "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
            "--strategy=TestRunner=standalone", "--nocache_test_results", "--flaky_test_attempts=1",
            "--test_timeout=900", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_env=CGROUPV2=true",
            "--test_output=errors", "--test_filter=" + pattern, "--test_arg=-test.run=" + pattern,
            "--build_event_json_file=" + str(bep), "--noshow_progress", "--color=no", "--curses=no"]
    if owners == [OWNER]:
        args.append("--run_under=/usr/bin/python3 -IB " + str(RUNNER) + " under")
    with (WORK / (name + ".log")).open("w") as out:
        result["exit"] = subprocess.run([*args, *owners], stdout=out, stderr=subprocess.STDOUT).returncode
    return bep


def outputs(owner, result):
    files = {}
    for e in result["testActionOutput"]:
        n = e["name"]
        if n in {"test.log", "test.xml"}:
            uri = urlparse(e["uri"])
            p = Path(unquote(uri.path))
            assert n not in files and uri.scheme == "file"
            assert str(p).endswith("/" + owner[2:].replace(":", "/") + "/" + n)
            assert not e.get("digest") or H["digest"](p) == e["digest"]
            files[n] = p
    assert set(files) == {"test.log", "test.xml"}
    return files


def components(row):
    pattern = "^(" + "|".join(value[0] for value in TARGETS.values()) + ")$"
    bep = run("components", list(TARGETS), pattern, row)
    rows = row["owners"] = {}
    for line in bep.read_text().splitlines():
        e = json.loads(line)
        target = e.get("id", {}).get("testResult", {}).get("label")
        if target not in TARGETS or "testResult" not in e:
            continue
        assert target not in rows
        result = e["testResult"]
        out = rows[target] = {"status": result["status"], "strategy": result["executionInfo"]["strategy"]}
        files = outputs(target, result)
        out["sha256"] = {n: H["digest"](p) for n, p in files.items()}
        log = files["test.log"].read_text(errors="replace")
        out["cases"] = {}
        for el in ET.parse(files["test.xml"]).iter("testcase"):
            name = el.attrib["name"].removeprefix(target.rsplit("/", 1)[-1].split(":")[0] + ".")
            assert name not in out["cases"]
            out["cases"][name] = ("failed" if el.find("failure") is not None or el.find("error") is not None else
                                  "skipped" if el.find("skipped") is not None else "pass")
        root, leaves = TARGETS[target]
        expected = {root, *(root + "/" + leaf for leaf in leaves)}
        out["verified"] = (result["status"] == "PASSED" and not result.get("cachedLocally") and
                           out["strategy"] in {"standalone", "local"} and set(out["cases"]) == expected and
                           all(s == "pass" and "--- PASS: " + n in log for n, s in out["cases"].items()))
        if not out["verified"]:
            out["diagnostics"] = H["fail_text"](log)
    return row["exit"] == 0 and rows.keys() == TARGETS.keys() and all(r["verified"] for r in rows.values())


def guest(arm, row):
    if arm == "positive":
        pattern = "^(" + "|".join(H["FAMILIES"]) + ")$"
    elif arm == "parent-command":
        pattern = "^TestRootfsUpperExecute$"
    else:
        pattern = H["CHECKPOINT_FILTER"]
    bep = run(arm, [OWNER], pattern, row)
    verified = H["collect"](bep, row, arm)
    if arm == "parent-command":
        return verified and row["exit"] == 3
    if arm == "positive":
        return verified and row["exit"] == 0
    events = (json.loads(s) for s in bep.read_text().splitlines())
    record, = [e["testResult"] for e in events if "testResult" in e and e["id"]["testResult"]["label"] == OWNER]
    log = outputs(OWNER, record)["test.log"].read_text(errors="replace")
    printed = re.search(r"CHECKPOINT_PRINT sha256=([0-9a-f]{64}) print=(pass|unreachable|other-error)\b", log)
    restored = re.search(r"CHECKPOINT_RESTORED sha256=([0-9a-f]{64}) pid=1 exe=test_app\b", log)
    row["same_image"] = bool(printed and restored and printed[1] == restored[1])
    if printed:
        row.update(checkpoint_sha256=printed[1], print_status=printed[2])
    return verified and row["exit"] == 0 and row["same_image"] and row["print_status"] == "pass"


def main():
    assert H["shell"]("git", "rev-parse", "HEAD") == SOURCE and H["shell"]("git", "rev-parse", "HEAD^") == PRODUCT
    assert CPU in H["MACHINES"]
    report = {"source": SOURCE, "standalone": PRODUCT, "arch": CPU, "kernel": platform.release(), "runs": {}}
    receipt = WORK / "result.json"
    tar = Path("runsc/cmd/tar_test.go")
    state = Path("runsc/cmd/statefile_test.go")
    tar_input, state_input = tar.read_bytes(), state.read_bytes()
    parent = subprocess.check_output(["git", "show", PRODUCT + ":" + str(tar)])
    old = b'spec := testutil.NewSpecWithArgs("/bin/sleep", "10000")'
    assert parent.count(old) == 1 and parent.replace(old, b"spec := idleGuestSpec(t)") == tar_input
    phases = ["components", "positive", "restored-1", "restored-2"]
    if CPU == "x86_64":
        phases.append("parent-command")
    for arm in phases:
        row = report["runs"][arm] = {"verified": False}
        try:
            if arm == "parent-command":
                tar.write_bytes(parent.replace(old, old + b"\n\tspec.Root.Path = idleGuestSpec(t).Root.Path"))
            elif arm.startswith("restored-"):
                assert H["shell"]("git", "hash-object", str(state)) == "f0c7e72d3d17c276cab093df75bcefe6cd5576c3"
                subprocess.run(["git", "apply", "--unidiff-zero", str(WORK / "inline_restore.patch")], check=True)
                assert H["shell"]("git", "hash-object", str(state)) == "788991771111d1160c3be78fe5745ce1f5a7ccd9"
                row["observed_test_source_sha256"] = H["digest"](state)
            row["verified"] = components(row) if arm == "components" else guest(arm, row)
        except (AssertionError, KeyError, OSError, ValueError, subprocess.CalledProcessError) as e:
            row["observer_error"] = type(e).__name__
        finally:
            tar.write_bytes(tar_input)
            state.write_bytes(state_input)
        if not row["verified"]:
            path = WORK / (arm + ".log")
            if path.exists():
                row["bazel_diagnostics"] = H["fail_text"](path.read_text(errors="replace"))
        receipt.write_text(json.dumps(report, indent=2))
    report["verified"] = all(r["verified"] for r in report["runs"].values())
    receipt.write_text(json.dumps(report, indent=2))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
