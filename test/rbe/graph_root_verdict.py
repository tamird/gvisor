import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from qualification_report import xml_cases

root = Path(sys.argv[1])
regression = "TestGraphBackingArrayAsRoot"
control = "TestGraphLoadNilRoot"
overflow = "TestGraphDecodeValueIntegerOverflow/int16"
unit = "//pkg/state:state_test"
broad = "//pkg/state/tests:tests_test"
lanes = {}
for lane, targets in (("stock", [unit, broad]), ("fix", [unit, broad])):
    folder = root / lane
    records = {}
    cases = {}
    diagnostic = ""
    array_controls = 0
    for target in targets:
        xml = folder / (target.split(":")[1] + ".xml")
        if not xml.exists():
            continue
        records[target] = xml_cases(xml)
        for case in ET.parse(xml).iter("testcase"):
            name = case.get("name", "")
            if target == broad and name.startswith("TestArrayPrimitives/") and all(case.find(k) is None for k in ("failure", "error", "skipped")):
                array_controls += 1
            if name not in (regression, control, overflow):
                continue
            cases[name] = "pass"
            if case.find("skipped") is not None:
                cases[name] = "skip"
            if (failure := case.find("failure")) is not None or (failure := case.find("error")) is not None:
                cases[name] = "fail"
                diagnostic = " ".join((failure.get("message", ""), failure.text or "", case.findtext("system-out", "")))
    rc = int((folder / "rc").read_text())
    tree = (folder / "tree").read_text().strip()
    want = {regression: "fail" if lane == "stock" else "pass", control: "pass", overflow: "pass"}
    valid = set(records) == set(targets) and cases == want and rc == (3 if lane == "stock" else 0) and tree == os.environ[lane.upper() + "_TREE"]
    symptom = "Load accepted a backing array as the root; destination = 7"
    original_observation = (folder / "accepted-without-assignment").exists() or symptom in diagnostic or any(symptom in p.read_text() for p in folder.glob("*.xml"))
    valid = valid and array_controls > 0 and records.get(broad, {}).get("failed") == 0
    if lane == "stock":
        valid = valid and original_observation and all(f.endswith(regression) for f in records.get(unit, {}).get("failures", []))
    else:
        valid = valid and all(r["passed"] > 0 and r["failed"] == 0 for r in records.values())
    lanes[lane] = dict(tree=tree, bazel_exit=rc, expected_targets=targets, missing=sorted(set(targets)-set(records)), targets=records, observed=cases, expected=want, valid_array_cases=array_controls, accepted_without_assignment=original_observation, qualified=valid)
passed = all(lane["qualified"] for lane in lanes.values())
report = dict(verdict="REPRODUCED_AND_FIXED" if passed else "FAIL_OR_INCOMPLETE", source=os.environ["PRODUCT"], base=os.environ["BASE"], lanes=lanes, execution_finished=(root/"end").read_text().strip(), verified=datetime.now(timezone.utc).isoformat())
(root / "verdict.json").write_text(json.dumps(report, indent=2)+"\n")
print(json.dumps({lane: record["qualified"] for lane, record in lanes.items()}))
sys.exit(not passed)
