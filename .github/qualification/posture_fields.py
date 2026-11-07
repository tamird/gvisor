"""Recover the differing Go fields from the original ARM64 posture workload."""
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import xml.etree.ElementTree as ET

import print_owners as harness

WORK = harness.WORK
TEST = Path("runsc/cmd/bootposture_test.go")
SOURCE = "e34a3859cd0913d12f1952a2f51bc8e23aac3907"
MODES = ("default", "hostinet", "no_directfs", "no_directfs_hostinet", "fdlimit")
SENTINEL = "sandbox posture differs from expectation (-want +got):"
PATTERN = re.compile(r"POSTURE_FIELDS (TestBootPosture/(?:default|hostinet|no_directfs|no_directfs_hostinet|fdlimit)/sentry) ([A-Za-z_][A-Za-z0-9_.,]{0,1500})[ \t\r]*$", re.M)
H = harness.H


def field_observer() -> bytes:
    data = TEST.read_bytes()
    marker = b'if d := sandboxposture.Diff(sandboxposture.Expected(opts), got); d != "" {'
    assert data.count(marker) == 1
    imports = b'\t"reflect"\n\t"sort"\n\n\t"github.com/google/go-cmp/cmp"\n'
    assert data.count(b'\t"testing"\n') == 1
    data = data.replace(b'\t"testing"\n', b'\t"testing"\n' + imports)
    original = b't.Errorf("Sandbox %d: %s", pid, d)'
    assert data.count(original) == 1
    data = data.replace(marker, b'want := sandboxposture.Expected(opts)\n\t\tif d := sandboxposture.Diff(want, got); d != "" {')
    output = b'''probe := postureFieldNames{fields: make(map[string]bool)}
            sandboxposture.Diff(want, got, cmp.Reporter(&probe))
            var fields []string
            for field := range probe.fields {
                fields = append(fields, field)
            }
            sort.Strings(fields)
            allowed := make(map[string]bool)
            for _, class := range want.FDs.Classes {
                allowed[class] = true
            }
            for _, class := range got.FDs.Classes {
                if !allowed[class] {
                    t.Logf("POSTURE_EXTRA_FD %s %d %s", t.Name(), pid, postureFDCategory(class))
                }
            }
            t.Logf("POSTURE_FIELDS %s %s", t.Name(), strings.Join(fields, ","))
            '''
    return data.replace(original, output + original) + (WORK / "posture_fields.inc").read_bytes()


def collect(name: str, modes: tuple[str, ...], observed: bool) -> dict:
    row = {"status": "INCOMPLETE", "source": SOURCE, "test_sha256": H["digest"](TEST)}
    pattern = "^TestBootPosture$/^(" + "|".join(modes) + ")$/^(sentry|gofer)$"
    bep = harness.run(name, [harness.OWNER], pattern, row)
    events = [json.loads(line) for line in bep.open()]
    results = [e["testResult"] for e in events if "testResult" in e and e["id"]["testResult"]["label"].lstrip("@") == harness.OWNER]
    if len(results) != 1:
        row["attempts"] = len(results)
        return row
    result = results[0]
    info = result.get("executionInfo", {})
    row.update(outcome=result["status"], strategy=info.get("strategy"), cached=bool(result.get("cachedLocally") or info.get("cachedRemotely")))
    files = harness.outputs(harness.OWNER, result)
    row["output_sha256"] = {key: H["digest"](p) for key, p in files.items()}
    log = files["test.log"].read_text(errors="replace")
    witnesses = [json.loads(line.removeprefix("CMD_GUEST_ORIGINAL ")) for line in log.splitlines() if line.startswith("CMD_GUEST_ORIGINAL ")]
    row["guest_verified"] = (len(witnesses) == 1 and witnesses[0].get("private_namespaces") == ["mnt", "pid", "net", "uts", "ipc"]
                             and witnesses[0].get("sidecars") == 7 and witnesses[0].get("test_app", {}).get("machine") == 183)
    wanted = {f"TestBootPosture/{mode}/{role}" for mode in modes for role in ("sentry", "gofer")}
    cases = {}
    for case in ET.parse(files["test.xml"]).iter("testcase"):
        key = case.get("name", "").removeprefix("cmd.")
        if key not in wanted:
            continue
        assert key not in cases
        if case.find("failure") is not None or case.find("error") is not None:
            cases[key] = "FAIL"
        elif (case.find("skipped") is not None or case.get("status", "").lower() in {"notrun", "skipped"}
              or case.get("result", "").lower() in {"skipped", "suppressed"}):
            cases[key] = "SKIP"
        else:
            cases[key] = "PASS"
    row["cases"] = cases
    row["comparison_errors"] = log.count(SENTINEL)
    fields = {}
    for key, group in PATTERN.findall(log):
        values = sorted(set(group.split(",")))
        if key in wanted and all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", v) for v in values):
            if key in fields and fields[key] != values:
                raise ValueError("conflicting field witnesses")
            fields[key] = values
    row["differences"] = fields
    # Join events by PID only on the worker. A change in another process
    # must not be attributed to the Sentry that failed the comparison.
    extras = []
    for case, pid, category in re.findall(r"POSTURE_EXTRA_FD (TestBootPosture/[a-z_]+/sentry) ([0-9]+) ([a-z-]+)\s*$", log, re.M):
        if category not in {"unknown", "deleted", "regular", "directory", "fifo", "symlink", "block",
                            "socket", "null", "pipe", "timerfd", "pidfd", "other-memfd", "other-anon", "char", "other"}:
            raise ValueError("descriptor observer returned an unrecognized category")
        if case in wanted and "FDs.Classes" in fields.get(case, []):
            failures = re.findall(r"^POSTURE_FD_STAT " + re.escape(pid) + r" (not-found|permission|other) (true|false)\s*$", log, re.M)
            extras.append({"case": case, "class": category, "stat_failures": failures})
    row["extra_classes"] = extras
    for case, names in fields.items():
        if "FDs.Classes" in names and not any(extra["case"] == case for extra in extras):
            raise ValueError("descriptor comparison failed without an extra-class witness")
    if (row["strategy"] in ("local", "standalone") and not row["cached"] and row["guest_verified"]
        and cases.keys() == wanted and "SKIP" not in cases.values()):
        failed = {key for key, value in cases.items() if value == "FAIL"}
        if not failed and row["exit"] == 0 and row["outcome"] == "PASSED":
            row["status"] = "PASSED_CASES"
        elif failed and row["exit"] == 3 and row["outcome"] == "FAILED" and row["comparison_errors"] >= len(failed):
            if observed and fields.keys() == failed:
                row["status"] = "DIFFERENCE_CAPTURED"
            elif not observed:
                row["status"] = "ORIGINAL_COMPARISON_FAILED"
    return row


BOOT = Path("runsc/cmd/sentry/sentrycmd/boot.go")
UNMOUNT = Path("runsc/cmd/sandboxsetup/process.go")
PRODUCT = "5ca9588a6ca7e68eb4f65d9d1e5d017778932ae3"


def observe_process(boot: bytes, fixed: bool) -> bytes:
    # Retain exactly the handle returned for this helper, so Go GC cannot
    # close it in the stock arm. The policy and the child are unchanged.
    marker = b"\tif b.setUpRoot {\n\t\tif err := setUpChroot"
    assert boot.count(marker) == 1
    boot = boot.replace(marker, b"\tvar postureProcHandle *os.Process\n" + marker)
    marker = b"defer cmd.Wait()\n\t\t\t\tdefer w.Close()\n\t\t\t} else {"
    assert boot.count(marker) == 1
    boot = boot.replace(marker, marker + b"""
                postureProcHandle = cmd.Process
                if err := postureProcHandle.WithHandle(func(uintptr) {}); err != nil {
                    util.Fatalf("expected live proc umounter handle at Start: %v", err)
                }""")
    marker = b"\tif b.procMountSyncFD != -1 {\n\t\tl.PreSeccompCallback = func() {"
    assert boot.count(marker) == 1
    boot = boot.replace(marker, b"""
    if postureProcHandle == nil || b.procMountSyncFD < 0 {
        util.Fatalf("qualified default boot did not own an umounter and sync pipe")
    }
""" + marker)
    marker = b'\n\t\t}\n\t}\n\n\t// Prepare metrics.'
    assert boot.count(marker) == 1
    expected = b"os.ErrProcessDone" if fixed else b"nil"
    return boot.replace(marker, b"""
            if got := postureProcHandle.WithHandle(func(uintptr) {}); got != """ + expected + b""" {
                util.Fatalf("unexpected proc umounter handle state after reaping: %v", got)
            }
""" + marker)


def run_trials() -> bool:
    assert platform.machine() == "aarch64"
    assert H["shell"]("git", "rev-parse", "HEAD") == SOURCE
    before = {p: p.read_bytes() for p in (BOOT, UNMOUNT, TEST)}
    corrected = {p: (WORK / ("fixed-" + p.name)).read_bytes() for p in (BOOT, UNMOUNT)}
    report = {"source": SOURCE, "product": PRODUCT, "kernel": platform.release(), "trials": {}, "verified": False}
    dest = WORK / "field-results.json"
    def save() -> None:
        dest.write_text(json.dumps(report, indent=2) + "\n")
    save()
    try:
        # The controls only run default, which uses the no-reexec path.
        # A separate uninstrumented run also exercises the inherited-FD path.
        TEST.write_bytes(field_observer())
        for name, modes, fixed, controlled in [
            ("stock-owns-child", ("default",), False, True),
            ("fixed-owns-child", ("default",), True, True),
            ("fixed-original", MODES, True, False),
        ]:
            raw_boot = corrected[BOOT] if fixed else before[BOOT]
            BOOT.write_bytes(observe_process(raw_boot, fixed) if controlled else raw_boot)
            UNMOUNT.write_bytes(corrected[UNMOUNT] if fixed else before[UNMOUNT])
            if not controlled:
                TEST.write_bytes(before[TEST])
            row = collect(name, modes, controlled)
            row["runtime_file_sha256"] = {str(p): H["digest"](p) for p in (BOOT, UNMOUNT)}
            expected = {"TestBootPosture/default/sentry": ["FDs.Classes"]}
            if name == "stock-owns-child":
                row["verified"] = (row["status"] == "DIFFERENCE_CAPTURED" and row["differences"] == expected
                                   and row["extra_classes"] == [{"case":"TestBootPosture/default/sentry",
                                                                 "class":"pidfd","stat_failures":[]}])
            else:
                row["verified"] = row["status"] == "PASSED_CASES"
            report["trials"][name] = row
            save()
    finally:
        for p, b in before.items():
            p.write_bytes(b)
    report["verified"] = len(report["trials"]) == 3 and all(r["verified"] for r in report["trials"].values())
    save()
    return report["verified"]


if __name__ == "__main__":
    raise SystemExit(not run_trials())
