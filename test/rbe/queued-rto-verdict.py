"""Reconcile real TestResults and per-case XML; keep partial evidence."""
from pathlib import Path
from xml.etree import ElementTree as ET
import json
import os
import re

root = Path(os.environ["RUNNER_TEMP"])
arms = {}
for name in ("stock", "draft"):
    d = root / "records" / name
    if not (d / "exit").exists():
        arms[name] = {"complete": False, "reason": "no Bazel exit"}
        continue
    code = int((d / "exit").read_text())
    events = []
    bep = root / f"{name}.bep"
    if bep.exists():
        for line in bep.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if set(e.get("id", {})) & {"targetCompleted", "testSummary", "testResult"}:
                events.append(e)
    targets = {}
    for label in (d / "labels").read_text().splitlines():
        actual = {label}
        for e in events:
            if e["id"].get("targetCompleted", {}).get("label") == label:
                actual.update(c[key]["label"] for c in e.get("children", []) for key in ("targetCompleted", "testSummary", "testResult") if c.get(key, {}).get("label"))
        key = label.split(":")[1]
        status = [e["testResult"]["status"] for e in events if e["id"].get("testResult", {}).get("label") in actual]
        summary = [e["testSummary"]["overallStatus"] for e in events if e["id"].get("testSummary", {}).get("label") in actual]
        dest = d / key
        log = (dest / "test.log").read_text(errors="replace") if (dest / "test.log").exists() else ""
        cases = {}
        try:
            for c in ET.parse(dest / "test.xml").iter("testcase"):
                cases[c.get("name")] = [t.tag for t in c if t.tag in ("failure", "skipped", "error")]
        except (ET.ParseError, OSError):
            pass
        passed = status == ["PASSED"] and summary == ["PASSED"] and cases == {"TestInterruptedRTOWithQueuedData": []}
        trace = [json.loads(line.split("QUEUED ", 1)[1]) for line in log.splitlines() if re.search(r"QUEUED [{]", line)]
        phases = [r["phase"] for r in trace]
        first = None
        if trace:
            old_end = trace[0]["old_end"]
            for row in trace:
                if any(right > old_end for left, right in row["packets"] or []):
                    first = {key: row[key] for key in ("phase", "ack", "clock_ns", "packets", "timeouts")}
                    first["before_old_flight_acked"] = row["ack"] < old_end
                    break
        passed &= (phases[:5] == ["original", "sack_recovery", "timeout", "queued", "partial_ack"]
                   and phases[-1:] == ["finished"] and first is not None
                   and [name for name in phases if name.startswith("timer_")] == [f"timer_{i}" for i in range(12)]
                   and len({r["timeouts"] for r in trace[2:phases.index("release_old")]}) == 1)
        raw = (d / "log").read_text(errors="replace")
        diag = [line.strip()[:520] for line in (log+"\n"+raw).splitlines()
                if re.search(r"^ERROR:|^Error:|^panic:|tcp_rack_test.go:[0-9]+:", line)
                and not re.search(r"token|authorization|api[_-]?key|bearer|header|password|secret", line, re.I)]
        targets[label] = dict(passed=passed, action=status, summary=summary, xml=len(cases), skips=sum("skipped" in v for v in cases.values()), trace=trace, first_new_sequence=first, diagnostics=diag[-15:])
    arms[name] = {"complete": code == 0 and bool(targets) and all(x["passed"] for x in targets.values()), "source": (d / "head").read_text().strip(), "targets": targets, "exit": code, "uuid": (d / "uuid").read_text().strip()}
v = {"complete": all(a["complete"] for a in arms.values()), "arms": arms}
(root / "records/verdict.json").write_text(json.dumps(v, indent=2)+"\n")
raise SystemExit(not v["complete"])
