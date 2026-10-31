#!/usr/bin/env python3
"""Verify the captured replay and owning analyzers."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

from qualification_report import xml_cases

root = Path(sys.argv[1])
base = "0bbcbf9de7626fcecba97f37723dce80b435d73d"
control = "3c7d2b027d2077ee98382eedbaa9a7d2b39235e7"
fixed = "7ab597c1c75ab15ae9634ee493e3fd1457bc162a"
target = "//pkg/fdnotifier:fdnotifier_test"
cases = ["TestStaleEpollEvent/ReusedFD","TestStaleEpollEvent/RearmedFD"]
diagnostic = "event from the old epoll registration notified the new waiter"
owners = ["fdnotifier_nogo","fdnotifier_test_nogo"]

def read(d, key):
    file = d / key
    return file.read_text(errors="replace").strip() if file.is_file() else ""

def reported_xml(file, errors):
    try:
        return xml_cases(file)
    except (OSError, ValueError, SyntaxError) as err:
        errors.append(f"{file.name}: {err}")
        return {}

def source(d, expected, errors):
    obj = (d/"commit").read_bytes() if (d/"commit").is_file() else b""
    sha = hashlib.sha1(f"commit {len(obj)}\0".encode()+obj).hexdigest()
    if sha != expected or read(d,"source") != expected or read(d,"source-after") != expected or read(d,"parent") != base:
        errors.append("unexpected commit, parent or source identity")
    if not (d/"source-before-status").is_file() or not (d/"source-after-status").is_file() or read(d,"source-before-status") or read(d,"source-after-status"):
        errors.append("missing or dirty tracked source")
    if not read(d,"start") or not read(d,"end"):
        errors.append("missing operation timestamps")
    return {"source":read(d,"source"),"parent":read(d,"parent"),"start":read(d,"start"),"end":read(d,"end"),"bazel_exit":read(d,"exit"),"argv":read(d,"argv").splitlines()}

def verify(arm, expected):
    d = root / arm
    errors = []
    row = source(d,expected,errors)
    positive = arm == "correction"
    parsed = {}
    log = read(d,"test.log")
    mark = "PASS" if positive else "FAIL"
    for name in cases:
        results = re.findall(r"--- (PASS|FAIL|SKIP): "+re.escape(name)+r"(?=\s)",log)
        launches = re.findall(r"(?m)^=== RUN\s+"+re.escape(name)+r"$",log)
        part = log.partition(launches[0])[2].split("=== RUN",1)[0] if len(launches)==1 else ""
        failures = re.findall(r"fdnotifier_test[.]go:\d+: ([^\n]+)", part)
        if results != [mark] or len(launches)!=1 or failures != ([] if positive else [diagnostic]):
            errors.append(f"{name}: results={results}, case starts={len(launches)}, diagnostics={failures}")
        parsed[name] = {"results":results,"diagnostics":failures}
    case_results = re.findall(r"--- (PASS|FAIL|SKIP): (\S+)",log)
    if sorted(case_results) != sorted([(mark,"TestStaleEpollEvent"),*((mark,c) for c in cases)]):
        errors.append(f"unexpected additional or missing Go results: {case_results}")
    if row["bazel_exit"] != ("0" if positive else "3") or row["argv"].count(target)!=1:
        errors.append("unexpected original Bazel exit or target")
    xml = reported_xml(d/"test.xml",errors)
    if xml.get("skipped") != 0 or (positive and (xml.get("failed")!=0 or not xml.get("passed"))) or (not positive and not xml.get("failed")):
        errors.append("XML disagrees with the Go assertion verdict")
    row.update({"cases":parsed, "missing":[c for c in cases if not parsed[c]["results"]], "test_xml":xml,
                "fixture":read(d,"fixture-sha256"),"errors":errors})
    return row

rows = {arm:verify(arm,sha) for arm,sha in (("control",control),("correction",fixed))}
errors = [e for r in rows.values() for e in r["errors"]]
if not rows["control"]["fixture"] or rows["control"]["fixture"]!=rows["correction"]["fixture"]:
    errors.append("different or missing test sources")
d = root/"owners"
issues = []
analysis = source(d,fixed,issues)
analysis["xml"] = {}
analysis["findings"] = {}
for owner in owners:
    label = f"//pkg/fdnotifier:{owner}"
    xml = reported_xml(d/(owner+".xml"),issues)
    analysis["xml"][owner]=xml
    if not xml.get("passed") or xml.get("failed") or xml.get("skipped") or analysis["argv"].count(label)!=1:
        issues.append(f"{owner}: missing/pending/failed/skipped owner")
    for arch in ("amd64","arm64"):
        name = f"{owner}.{arch}.findings"
        digest=read(d,name+".sha256")
        if not re.fullmatch(r"[a-f0-9]{64}",digest): issues.append("missing "+name)
        analysis["findings"][name]=digest
if analysis["bazel_exit"]!="0": issues.append("analyzer exit is not zero")
analysis["errors"]=issues
errors.extend(issues)
verdict={"ok":not errors,"source_base":base,"target":target,"cases":cases,"arms":rows,"owners":analysis,"errors":errors,
         "verified_at":datetime.now(timezone.utc).isoformat()}
(root/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
print(json.dumps(verdict))
sys.exit(bool(errors))
