"""Check named packet executions and preserve distinct baseline/corrected observations."""
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

root = Path(os.environ["RUNNER_TEMP"])
out = {}
for arm in ("master-clock", "fixed-clock"):
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
        records = [e["testResult"] for e in events if e["id"].get("testResult", {}).get("label") in actual]
        action = [r.get("status") for r in records]
        summary = [e["testSummary"].get("overallStatus") for e in events if e["id"].get("testSummary", {}).get("label") in actual]
        executors = [r.get("executionInfo", {}).get("strategy") for r in records]
        cached = any(r.get("cachedLocally") or r.get("executionInfo", {}).get("cachedRemotely") for r in records)
        dest = d / label.split(":")[1]
        text = (dest / "test.log").read_text(errors="replace")
        cases = {}
        try:
            for case in ET.parse(dest / "test.xml").iter("testcase"):
                cases[case.get("name", "")] = [c.tag for c in case if c.tag in ("failure", "error", "skipped")]
        except (OSError, ET.ParseError):
            pass
        passed = action == summary == ["PASSED"] and not cached and code == 0
        named = {k: v for k, v in cases.items() if "Fresh" in k}
        trace = [s.strip() for s in text.splitlines() if any(event in s for event in ("WIRE schedule=", "TIMER immediate=", "BEFORE_NEXT_RTO", "NEXT_RTO elapsed="))]
        schedules = ("fresh-new-sack", "fresh-reorder-timer")
        for schedule in schedules:
            matching = [v for k,v in named.items() if k.endswith("/" + schedule)]
            passed &= matching == [[]] and sum("WIRE schedule=" + schedule + " " in line for line in trace) == 1
        passed &= sum("NEXT_RTO elapsed=" in line and "BEFORE_NEXT_RTO elapsed=" not in line for line in trace) == 1
        passed &= len([k for k in named if "/" in k]) == 2
        source = (d / "head").read_text().strip()
        passed &= source == os.environ["MASTER" if arm == "master-clock" else "PRODUCT"]
        results[label] = {"passed":passed, "actual":sorted(actual), "action":action, "summary":summary, "strategies":executors, "cached":cached, "xml":len(cases), "skips":sum("skipped" in v for v in cases.values()), "named":named, "observations":trace}
    out[arm] = {"passed": set(results) == {"//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test"} and all(r["passed"] for r in results.values()), "source":(d/"head").read_text().strip(), "exit":code, "targets":results, "invocations":re.findall(r"Invocation ID: ([\w-]+)", log)}
record = {"fixture":hashlib.sha256((root/"rack-fresh-head.go.txt").read_bytes()).hexdigest(), "execution_finished":(root/"records/finished").read_text().strip() if (root/"records/finished").exists() else None, "verified_at":datetime.now(timezone.utc).isoformat(), "arms":out, "complete":all(r["passed"] for r in out.values())}
(root/"records/verdict.json").write_text(json.dumps(record,indent=2)+"\n")
raise SystemExit(not record["complete"])
