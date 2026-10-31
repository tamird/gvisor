from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from xml.etree import ElementTree

from qualification_report import xml_cases

root = Path(sys.argv[1])
unit = "//pkg/fdnotifier:fdnotifier_test"
consumer = "//pkg/sentry/fsimpl/eventfd:eventfd_test"
owners = ["//pkg/fdnotifier:fdnotifier_nogo", "//pkg/fdnotifier:fdnotifier_test_nogo"]
errors = []
rows = {}
for lane, targets in [("stock", [unit]), ("fix", [unit, consumer]), ("owners", owners)]:
    folder = root / lane
    tree = (folder / "tree").read_text().strip()
    rc = int((folder / "rc").read_text())
    observed = {}
    findings = {}
    diagnostic = (folder / "diagnostics").read_text()
    if tree != os.environ["STOCK_TREE" if lane == "stock" else "FIX_TREE"] or (folder / "expected").read_text().splitlines() != targets:
        errors.append(f"{lane}: source/targets do not match")
    if rc != (3 if lane == "stock" else 0):
        errors.append(f"{lane}: unexpected exit {rc}")
    for target in targets:
        name = target.split(":")[1]
        xml = folder / (name + ".xml")
        if not xml.is_file():
            errors.append(f"{lane}: missing {target}")
            continue
        counts = xml_cases(xml)
        cases = {c.get("name"): ("fail" if any(c.find(key) is not None for key in ("failure", "error")) else "skip" if c.find("skipped") is not None else "pass") for c in ElementTree.parse(xml).iter("testcase")}
        observed[target] = dict(**counts, cases=cases)
        if target == unit:
            expected = "fail" if lane == "stock" else "pass"
            for case in ("TestNotifierStartup/Pause", "TestNotifierStartup/Register"):
                if cases.get(case) != expected:
                    errors.append(f"{lane}: {case} did not {expected}")
            if lane == "stock":
                log = (folder / (name + ".log")).read_text()
                for symptom in ("nil pointer dereference", "fdnotifier.Pause", "AddFD after restoring the descriptor limit: too many open files"):
                    if symptom not in log:
                        errors.append(f"stock: missing {symptom}")
                if any(not n.endswith(("TestNotifierStartup","TestNotifierStartup/Pause","TestNotifierStartup/Register")) for n in counts["failures"]):
                    errors.append("stock: unrelated failure")
        if lane != "stock" and (counts["passed"] == 0 or counts["failed"] or counts["skipped"]):
            errors.append(f"{lane}: incomplete or failing {target}")
        if target == consumer and any(cases.get(k) != "pass" for k in ["TestEventFD","TestEventFDStat"]):
            errors.append("eventfd consumer cases did not execute")
        if lane == "owners":
            for arch in ("amd64", "arm64"):
                digest = folder / f"{name}.{arch}.sha256"
                if digest.is_file() and re.fullmatch(r"[0-9a-f]{64}", digest.read_text().split()[0]):
                    findings[digest.name] = digest.read_text().split()[0]
                else:
                    errors.append(f"missing findings for {target}/{arch}")
    rows[lane] = dict(tree=tree, expected=targets, missing=sorted(set(targets)-set(observed)), rc=rc, results=observed, findings=findings, diagnostics=diagnostic.splitlines())

report = dict(verdict="REPRODUCED_AND_FIXED" if not errors else "FAIL_OR_INCOMPLETE", source=os.environ["PRODUCT"], base=os.environ["BASE"], lanes=rows, execution_finished=(root/"end").read_text().strip(), verified=datetime.now(timezone.utc).isoformat(), errors=errors)
(root / "verdict.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(errors))
sys.exit(bool(errors))
