#!/usr/bin/python3 -B
"""Compare every generated Go testcase on both source revisions and target CPUs."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

import qualification_report as report


STOCK = "451b92800b9cb82262dc8a735388f7cf4d17d895"
PRODUCT = "dbdd8a0946f8b0615b4cf58b51369efc58240fc5"
HERE = Path(__file__).resolve().parent
OUT = HERE / "receipt"
QEMU = HERE / "qemu-user-linux-amd64-aarch64"
CLI = [str(HERE / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g"]
PACKAGES = """pkg/abi/gasket pkg/abi/ib pkg/abi/linux pkg/abi/nvgpu pkg/erofs pkg/lisafs
pkg/marshal/primitive pkg/sentry/arch pkg/sentry/arch/fpu pkg/sentry/fsimpl/fuse
pkg/sentry/fsimpl/tmpfs pkg/sentry/kernel pkg/sentry/kernel/auth pkg/sentry/loader
pkg/sentry/platform/systrap/usertrap pkg/sentry/seccheck/sinks/remote/wire
pkg/sentry/socket/netfilter pkg/sentry/syscalls/linux tools/go_marshal/test
tools/go_marshal/test/external""".split()
LIBRARIES = {"//" + p + ":" + p.rsplit("/", 1)[1] for p in PACKAGES}
MARKER = "MARSHAL_CASE_ELF "
RUN = re.compile(r"^=== RUN\s+(\S+)$")
DONE = re.compile(r"^\s*--- (PASS|SKIP|FAIL): (\S+) \(")


def read(cmd):
    return subprocess.check_output(cmd, text=True).strip()


def save(name, value):
    (OUT / (name + ".json")).write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def machine(path):
    with path.open("rb") as stream:
        hdr = stream.read(20)
    assert hdr[:6] == b"\x7fELF\x02\x01"
    return int.from_bytes(hdr[18:20], "little")


def probe(cpu, program, args):
    path = Path(program).resolve(strict=True)
    assert os.uname().machine == "x86_64"
    elf = machine(path)
    assert elf == {"amd64": 62, "arm64": 183}[cpu]
    marker = {"owner": os.environ["TEST_TARGET"], "machine": elf, **report.file_record(path)}
    print(MARKER + json.dumps(marker), flush=True)
    if cpu == "arm64":
        os.execv(QEMU, [str(QEMU), "-L", "/usr/aarch64-linux-gnu", str(path), *args])
    os.execv(path, [str(path), *args])


def selected(stock):
    scope = " ".join("//" + p + ":all" for p in PACKAGES)
    query = 'filter("_abi_autogen_test$", set(' + scope + "))"
    labels = read([*CLI, "query", "--lockfile_mode=off", "--output=label", query]).splitlines()
    owners = {}
    for label in labels:
        pkg = label.split(":")[0]
        lib = pkg + ":" + pkg.rsplit("/", 1)[1]
        assert lib in LIBRARIES and label.startswith(lib + "_") and label not in owners
        owners[label] = lib
    assert len(owners) == (46 if stock else 20)
    assert {lib + "_abi_autogen_test" for lib in LIBRARIES} <= owners.keys()
    return owners


def output_file(results, name, owner):
    item, = [item for item in results if item["name"] == name]
    uri = urlsplit(item["uri"])
    path = Path(unquote(uri.path))
    assert uri.scheme == "file" and not uri.netloc
    assert str(path).endswith("/testlogs/" + owner[2:].replace(":", "/") + "/" + name)
    record = report.file_record(path)
    if item.get("digest"):
        assert item["digest"] == record["sha256"]
    if "length" in item:
        assert int(item["length"]) == record["bytes"]
    return path, record


def observe(event, owner, cpu):
    result = event["testResult"]
    info = result.get("executionInfo", {})
    key = event["id"]["testResult"]
    log, log_id = output_file(result["testActionOutput"], "test.log", owner)
    xml, xml_id = output_file(result["testActionOutput"], "test.xml", owner)
    row = {"configuration": key["configuration"]["id"], "status": result["status"],
           "strategy": info.get("strategy"), "cached": bool(result.get("cachedLocally") or info.get("cachedRemotely")),
           "log": log_id, "xml": xml_id, "cases": {}, "duplicate": [], "problems": []}
    lines = log.read_text(errors="replace").replace("\x16", "").splitlines()
    starts = Counter()
    ends = Counter()
    for line in lines:
        if line.startswith(MARKER):
            assert "elf" not in row
            row["elf"] = json.loads(line[len(MARKER):])
        if m := RUN.fullmatch(line):
            starts[m[1]] += 1
        if m := DONE.match(line):
            ends[m[2]] += 1
            row["cases"][m[2]] = m[1]
    row["duplicate"] = sorted(n for n in starts.keys() | ends.keys() if starts[n] > 1 or ends[n] > 1)
    if starts != ends or row["duplicate"]:
        row["problems"].append("unmatched or duplicate Go starts/outcomes")
    if not any(line == "PASS" for line in lines) or not row.get("elf"):
        row["problems"].append("missing Go pass or executable witness")
    if row.get("elf", {}).get("owner") != owner or row.get("elf", {}).get("machine") != {"amd64": 62, "arm64": 183}[cpu]:
        row["problems"].append("executable owner or machine mismatch")
    counts = report.xml_cases(xml)
    row["xml_counts"] = {key: counts[key] for key in ("passed", "failed", "skipped")}
    if counts["failed"] or any(status != "PASS" for status in row["cases"].values()):
        row["problems"].append("failures or skips")
    if cpu == "amd64":
        xml_names = Counter(case.get("name") for case in ET.parse(xml).iter("testcase"))
        if xml_names != ends or counts["passed"] != len(ends):
            row["problems"].append("Go case names and rules_go XML differ")
    if row["status"] != "PASSED" or row["strategy"] != "local" or row["cached"] or any(int(key.get(k, 1)) != 1 for k in ("run", "attempt", "shard")):
        row["problems"].append("Bazel attempt status")
    return row


def run(cell, cpu, labels, revision):
    bep = HERE / (cell + ".bep")
    console = HERE / (cell + ".log")
    argv = [*CLI, "test", "--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[cpu],
            "--remote_executor=", "--remote_cache=", "--bes_backend=", "--jobs=4", "--lockfile_mode=off",
            "--spawn_strategy=processwrapper-sandbox,local", "--strategy=GoStdlib=processwrapper-sandbox",
            "--strategy=GoStdlibList=processwrapper-sandbox", "--strategy=TestRunner=local",
            "--nocache_test_results", "--flaky_test_attempts=1", "--keep_going",
            "--run_under=" + str(HERE / "probe") + " " + cpu,
            *(["--test_env=GO_TEST_WRAP_TESTV=1"] if cpu == "amd64" else ["--test_env=GO_TEST_WRAP=0", "--test_arg=-test.v"]),
            "--build_event_json_file=" + str(bep), *sorted(labels)]
    observed = {"source": revision, "cpu": cpu, "tests": {}, "problems": []}
    completed = {}
    with console.open("wb") as f:
        observed["exit"] = subprocess.run(argv, stdout=f, stderr=subprocess.STDOUT).returncode
    if observed["exit"]:
        observed["diagnostics"] = [s[:350] for s in console.read_text(errors="replace").splitlines() if s.startswith("ERROR:")][:8]
    if bep.exists():
        observed["bep_sha256"] = report.file_record(bep)["sha256"]
        for line in bep.open():
            event = json.loads(line)
            if "started" in event:
                observed["invocation"] = event["started"]["uuid"]
            if "finished" in event:
                observed["bazel_exit"] = int(event["finished"]["exitCode"].get("code", 0))
            target = event["id"].get("targetCompleted", {})
            if target.get("label") in labels and not target.get("aspect") and event.get("completed", {}).get("success"):
                completed[target["label"]] = target["configuration"]["id"]
            if "testResult" not in event or "testResult" not in event["id"]:
                continue
            owner = event["id"]["testResult"]["label"]
            assert owner in labels and owner not in observed["tests"]
            row = {"library": labels[owner]}
            observed["tests"][owner] = row
            try:
                row.update(observe(event, owner, cpu))
            except (AssertionError, ValueError, OSError, KeyError, ET.ParseError) as err:
                row["problems"] = ["original readback " + type(err).__name__]
            if row["problems"]:
                observed["problems"].append(owner)
    observed["missing"] = sorted(labels.keys() - observed["tests"].keys())
    observed["unmatched_configurations"] = [owner for owner, row in observed["tests"].items()
        if not completed.get(owner) or completed[owner] != row.get("configuration")]
    observed["verified"] = (observed["exit"] == observed.get("bazel_exit") == 0 and not observed["missing"]
                            and not observed["problems"] and not observed["unmatched_configurations"])
    save(cell, observed)
    return observed


def flatten(cell):
    cases = {lib: {} for lib in LIBRARIES}
    duplicates = []
    for row in cell.get("tests", {}).values():
        lib = row["library"]
        for name, status in row.get("cases", {}).items():
            if name in cases[lib]:
                duplicates.append([lib, name])
            cases[lib][name] = status
    return cases, duplicates


def main():
    summary = {"source": PRODUCT, "parent": STOCK, "cells": {}, "comparisons": {}, "verified": False}
    save("summary", summary)
    assert read(["git", "rev-parse", "HEAD"]) == os.environ["GITHUB_SHA"]
    assert read(["git", "rev-parse", "HEAD^", "HEAD^^"]).splitlines() == [PRODUCT, STOCK]
    assert read(["git", "rev-parse", PRODUCT + "^{tree}"]) == "40c3756acbf7acd47b440a353ac8c89c36f3db11"
    assert read(["git", "diff", "--name-only", STOCK, PRODUCT]).splitlines() == ["tools/defs.bzl"]
    assert read([str(HERE / "bazel"), "--version"]) == "bazel 9.3.1"
    summary["emulator"] = {**report.file_record(QEMU), "version": read([str(QEMU), "--version"]).splitlines()[0]}
    assert machine(QEMU) == 62
    cells = {}
    for arm, revision in (("stock", STOCK), ("candidate", PRODUCT)):
        read(["git", "checkout", "-q", "--detach", revision])
        assert not read(["git", "diff", "--name-only", "HEAD"])
        summary[arm + "_tree"] = read(["git", "rev-parse", "HEAD^{tree}"])
        labels = selected(arm == "stock")
        for cpu in ("amd64", "arm64"):
            name = arm + "-" + cpu
            summary["stage"] = name
            save("summary", summary)
            try:
                observed = run(name, cpu, labels, revision)
                cells[name] = observed
                summary["cells"][name] = observed["verified"]
            except (AssertionError, ValueError, KeyError, OSError) as err:
                summary["cells"][name] = False
                summary.setdefault("collector_errors", {})[name] = type(err).__name__
            save("summary", summary)
    for cpu in ("amd64", "arm64"):
        a, d1 = flatten(cells.get("stock-" + cpu, {}))
        b, d2 = flatten(cells.get("candidate-" + cpu, {}))
        delta = {}
        for lib in sorted(LIBRARIES):
            if a[lib] != b[lib]:
                delta[lib] = {k: [a[lib].get(k), b[lib].get(k)] for k in sorted(a[lib].keys() | b[lib].keys()) if a[lib].get(k) != b[lib].get(k)}
        summary["comparisons"][cpu] = {"counts": {lib: [len(a[lib]), len(b[lib])] for lib in sorted(a)},
                                       "differing_cases": delta, "duplicates": [d1, d2],
                                       "identical": not (delta or d1 or d2) and any(a.values())}
    summary["verified"] = all(summary["cells"].values()) and len(summary["cells"]) == 4 and all(x["identical"] for x in summary["comparisons"].values())
    summary["stage"] = "finished"
    save("summary", summary)
    return summary["verified"]


if __name__ == "__main__":
    if sys.argv[1:] == ["compare"]:
        sys.exit(0 if main() else 1)
    probe(sys.argv[1], sys.argv[2], sys.argv[3:])
