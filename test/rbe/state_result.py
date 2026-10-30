from datetime import datetime, timezone
from pathlib import Path
import json
import sys

mode, dirname, *rest = sys.argv[1:]
root = Path(dirname)
if mode == 'context':
    context = dict(expected_source=rest[0], commit_file='commit', source_before='before', source_after='after', final_head='head', executed_arguments='arguments', native_exit='exit', capture_exit='capture-exit', execution_finished_at=(root/'finish').read_text().strip(), command_kind='test')
    (root/'context.json').write_text(json.dumps(context)+'\n')
    sys.exit()

from xml.etree import ElementTree as ET

expected = {
    "//pkg/state:state_test": {
        "TestLoadValueSnapshot", "TestLoadValueSnapshot/wire", "TestLoadValueSnapshot/graph",
        "TestZeroSizedSliceLargeCapacity", "TestPooledEncoderScratchReleasesPointers",
    },
    "//pkg/state/tests:tests_test": {
        "TestNestedValueConversion", "TestNestedValueConversion/wire", "TestNestedValueConversion/graph",
    },
}
directory = root / "focused"
verdict = json.loads((directory / "verdict.json").read_text())
issues = list(verdict["errors"])
observed = {}
for run in verdict["executed"]:
    label = run["owner"]["label"]
    if label in observed:
        issues.append(f"duplicate executed target {label}")
    if (run["status"] != "PASSED" or run["cached_locally"] or
            run["execution_info"].get("strategy") != "remote" or run["execution_info"].get("cachedRemotely")):
        issues.append(f"not a passing uncached remote target: {label}")
    package, target = label.removeprefix("//").split(":")
    xml = list((directory / "original").glob(f"**/testlogs/{package}/{target}/test.xml"))
    if len(xml) != 1:
        issues.append(f"missing or duplicate original XML: {label}")
        continue
    observed[label] = {}
    for case in ET.parse(xml[0]).iter("testcase"):
        name = case.get("name")
        if name in observed[label]:
            issues.append(f"duplicate case: {label} {name}")
        observed[label][name] = not any(child.tag in ("skipped", "error", "failure") for child in case)
    if set(observed[label]) != expected.get(label) or not all(observed[label].values()):
        issues.append(f"missing, additional or unsuccessful case: {label}")
if set(observed) != set(expected) or set(verdict["expected_labels"]) != set(expected):
    issues.append("expected both state packages")
if (verdict["source"] != "6f68b9196227e6899ce3221990677159ef1c17e0" or verdict["missing"] or
        verdict["native_exit"] != 0 or verdict["status"] != "PASS" or verdict["case_counts"] != {"passed": 8, "failed": 0, "skipped": 0}):
    issues.append("verdict did not record eight passing case entries and the exact proposed head")
summary = dict(source=verdict["source"], native_exit=verdict["native_exit"], cases=observed,
               finished_at=verdict["execution_finished_at"], issues=issues)
(root / "named-cases.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary))
raise SystemExit(1 if issues else 0)
