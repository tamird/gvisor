"""Check named packet executions and preserve distinct baseline/corrected observations."""
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

root = Path(os.environ["RUNNER_TEMP"])
out = {}
for arm in ("linux", "stock", "netstack", "prbase"):
    d = root / "records" / arm
    if not (d / "exit").exists():
        out[arm] = {"passed": False, "reason": "not completed"}
        continue
    code = int((d / "exit").read_text())
    log = (d / "log").read_text(errors="replace")
    bep = root / (arm + ".bep")
    events = []
    if bep.exists():
        with bep.open() as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    results = {}
    for label in (d / "labels").read_text().splitlines():
        actual = {label}
        for e in events:
            target = e["id"].get("targetCompleted", {}).get("label")
            if target == label:
                for child in e.get("children", []):
                    for k in ("targetCompleted", "testSummary", "testResult"):
                        if "label" in child.get(k, {}):
                            actual.add(child[k]["label"])
        action = [e["testResult"].get("status") for e in events if e["id"].get("testResult", {}).get("label") in actual]
        summary = [e["testSummary"].get("overallStatus") for e in events if e["id"].get("testSummary", {}).get("label") in actual]
        dest = d / label.split(":")[1]
        text = (dest / "test.log").read_text(errors="replace")
        cases = {}
        try:
            for case in ET.parse(dest / "test.xml").iter("testcase"):
                cases[case.get("name", "")] = [c.tag for c in case if c.tag in ("failure", "error", "skipped")]
        except (OSError, ET.ParseError):
            pass
        named = {k: v for k, v in cases.items() if "TestSACKCreditBeforeCumulativeACK" in k}
        trace = [s.strip() for s in text.splitlines() if "CREDIT mode=" in s]
        passed = action == summary == ["PASSED"] and code == 0
        for case in ("open", "closed"):
            passed &= any(k.endswith("/" + case) and not v for k, v in named.items())
            passed &= len([s for s in trace if "CREDIT mode=" + case + " " in s]) == 1
        note = [s.strip()[:520] for s in (text + "\n" + log).splitlines() if re.search(r"CREDIT mode=|tcp_rack_test.go:\d+|^ERROR:|^panic:|^--- FAIL|^FAIL:|^--- SKIP|failed to|disable RACK|check RACK|command returned|operation not permitted|read-only file system", s) and not re.search("token|authorization|api[_-]?key|bearer|remote[_-]?header|password",s,re.I)]
        results[label] = {"passed":passed, "actual":sorted(actual), "action":action, "summary":summary, "xml":len(cases), "skips":sum("skipped" in v for v in cases.values()), "named":named, "observations":trace, "diagnostics":note[-24:] if not passed or arm=="stock" else []}
    out[arm] = {"passed": all(r["passed"] for r in results.values()), "source":(d/"head").read_text().strip(), "exit":code, "targets":results, "invocations":re.findall(r"Invocation ID: ([\w-]+)", log)}
record = {"fixture":hashlib.sha256((root/"rack-fresh-head.go.txt").read_bytes()).hexdigest(), "arms":out, "complete":all(r["passed"] for r in out.values())}
(root/"records/verdict.json").write_text(json.dumps(record,indent=2)+"\n")
raise SystemExit(not record["complete"])
