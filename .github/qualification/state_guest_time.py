"""Run the retained native checkpoint fixture on both decoders in balanced pairs."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
from xml.etree import ElementTree

phase, sys.argv[1] = sys.argv[1], "helpers"
driver = runpy.run_path(str(Path(__file__).with_name("pair-run.py")))
out, worker = driver["out"], driver["gnu"]
label = "//runsc/container:container_test"
case = "TestCheckpointTmpfsTreeForProfile"
sources = driver["sources"]
pairs = 8
# Pair eight warms both binaries and both build graphs outside the timed cohort.
expected = [(pairs, "stock"), (pairs, "fixed")] + [
    (i, arm) for i in range(pairs) for arm in driver["order"](i)
]

if phase == "run":
    for i, arm in expected:
        try:
            driver["live"](i, arm)
        finally:
            # Keep Bazel's status if a failed test also failed to write XML.
            xml = subprocess.run(worker + [
                "cat", "bazel-testlogs/runsc/container/container_test/test.xml",
            ], capture_output=True)
            if xml.returncode == 0:
                (out / f"live-{i:02d}-{arm}.xml").write_bytes(xml.stdout)
        # Preserve hashes in the receipt; each trial generates its own checkpoint.
        folder = driver["root"] / ("capture" if i == 0 and arm == "stock" else f"live-{i:02d}-{arm}")
        for filename in ("checkpoint.img", "pages.img", "pages_meta.img", "spec.json"):
            (folder / filename).unlink()
elif phase == "stats":
    driver["benchstat"]("stock.bench", "fixed.bench", "benchstat.txt")
elif phase == "verify":
    missing, failures, skips, executed = [], [], [], []
    samples = {"stock": [], "fixed": []}
    finished = []
    for i, arm in expected:
        name = f"live-{i:02d}-{arm}"
        command_path = out / (name + "-command.json")
        result_path = out / (name + ".json")
        xml_path = out / (name + ".xml")
        if command_path.exists():
            command = json.loads(command_path.read_text())
            finished.append(command["end"])
            if command["exit"]:
                failures.append(name)
        if not all(p.exists() for p in (command_path, result_path, xml_path)):
            missing.append(name)
            continue
        try:
            result = json.loads(result_path.read_text())
            tests = list(ElementTree.fromstring(xml_path.read_bytes()).iter("testcase"))
        except (ValueError, ElementTree.ParseError):
            missing.append(name)
            continue
        if [t.get("name") for t in tests] != [case]:
            missing.append(name)
            continue
        t = tests[0]
        if t.find("skipped") is not None or t.get("status") in ("notrun", "suppressed") or t.get("result") == "suppressed":
            skips.append(name)
            continue
        executed.append(name)
        ns = result.get("nanoseconds", {})
        if (t.find("failure") is not None or t.find("error") is not None or result.get("exit") != 0
                or result.get("source") != sources[arm] or result.get("arm") != arm or result.get("pair") != i
                or not 0 < ns.get("restore", 0) < ns.get("new-to-guest", 0)
                or set(result.get("files", {})) != {"checkpoint.img", "pages.img", "pages_meta.img", "spec.json"}):
            failures.append(name)
            continue
        if i < pairs:
            for metric in ("new-to-guest", "restore"):
                samples[arm].append(f"BenchmarkNative/{metric} 1 {ns[metric]} ns/op")
    complete = not missing and not failures and not skips and os.environ.get("RUN_OUTCOME") == "success"
    verdict = {"status": "PASS" if complete else ("FAIL" if failures else "INCOMPLETE"),
               "sources": sources, "callback_pr": os.environ["BASE"], "queue_pr": os.environ["QUEUE"],
               "combined": os.environ["PRODUCT"],
               "expected_target": label, "expected_case": case,
               "expected": [f"live-{i:02d}-{arm}" for i, arm in expected],
               "warmup_pair": pairs, "measured_pairs": pairs, "executed": executed,
               "failed": sorted(set(failures)), "skipped": skips, "missing": missing,
               "last_execution_finished_ns": max(finished, default=None), "verified_ns": time.time_ns()}
    driver["record"]("verdict.json", verdict)
    if not complete:
        raise SystemExit(1)
    for arm, rows in samples.items():
        (out / (arm + ".bench")).write_text("\n".join(rows) + "\n")
else:
    raise ValueError(phase)
