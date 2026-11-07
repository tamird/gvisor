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
        elif case.find("skipped") is not None or case.get("status") == "notrun":
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


assert platform.machine() == "aarch64"
assert H["shell"]("git", "rev-parse", "HEAD") == SOURCE
before = TEST.read_bytes()
report = {"source": SOURCE, "kernel": platform.release(), "trials": {}, "status": "INCOMPLETE"}
dest = WORK / "field-results.json"
dest.write_text(json.dumps(report))
try:
    for name, modes, observe in [("original", MODES, False), ("field", MODES, True), ("default-2", ("default",), True), ("default-3", ("default",), True)]:
        if name == "field":
            TEST.write_bytes(field_observer())
        try:
            row = collect(name, modes, observe)
        except (OSError, ValueError, KeyError, AssertionError, subprocess.SubprocessError) as exc:
            row = {"status": "INCOMPLETE", "error": type(exc).__name__}
        if row["status"] == "INCOMPLETE" and (raw := WORK / (name + ".log")).is_file():
            data = raw.read_text(errors="replace")
            row["diagnostics"] = [kind for kind, s in {"test compilation":"bootposture_test.go:", "dependency fetch":"Error downloading", "toolchain":"No matching toolchain", "sandbox startup":"sandbox exited before the container started", "reporter failure":"postureFieldNames"}.items() if s in data]
        report["trials"][name] = row
        dest.write_text(json.dumps(report, indent=2) + "\n")
        if row["status"] == "INCOMPLETE":
            break
finally:
    TEST.write_bytes(before)
if len(report["trials"]) == 4 and all(v["status"] != "INCOMPLETE" for v in report["trials"].values()):
    report["status"] = ("DIFFERENCE_CAPTURED" if any(v["status"] == "DIFFERENCE_CAPTURED" for v in report["trials"].values())
                        else "NO_INSTRUMENTED_MISMATCH")
dest.write_text(json.dumps(report, indent=2) + "\n")
print(report["status"], {k: v["status"] for k, v in report["trials"].items()})
raise SystemExit(report["status"] == "INCOMPLETE")
