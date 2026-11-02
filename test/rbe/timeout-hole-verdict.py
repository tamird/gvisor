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
    if not all((d / f).exists() for f in ("exit", "head", "labels", "uuid")):
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
        passed = status == ["PASSED"] and summary == ["PASSED"] and cases == {"TestRACKTimeoutHoleDeadline": []}
        trace = [json.loads(line.split("HOLE ", 1)[1]) for line in log.splitlines() if re.search(r"HOLE [{]", line)]
        phases = [r["phase"] for r in trace]
        by = {x["phase"]: x for x in trace if x["phase"] != "intervening"}
        expected = ["sent", "first_rto", "before_sack", "sack_d_f", "second_rto", "released", "complete"]
        passed &= [p for p in phases if p != "intervening"] == expected
        mechanism = {"matched": False, "reason": "missing packet phases"}
        if passed:
            first, sack, second = (by[k] for k in ("first_rto", "sack_d_f", "second_rto"))
            wires = sack["wires"] or []
            passed &= (by["sent"]["timeouts"] == 0 and first["timeouts"] == sack["timeouts"] == 1
                       and by["complete"]["timeouts"] == second["timeouts"] == 2
                       and 0 in (first["wires"] or []) and 0 in (second["wires"] or []))
            early = second["ns"] - first["ns"] - first["rto_ns"]
            later = second["ns"] - sack["ns"] - sack["rto_ns"]
            intervening = [r for r in trace if first["ns"] < r["ns"] < second["ns"] and r["phase"] == "intervening"]
            matched = (sack["ns"]-first["ns"] == 40_000_000 and 0 in wires and {1,2}.issubset(wires)
                       and first["rto_ns"] == sack["rto_ns"] and not by["before_sack"]["wires"]
                       and not any(0 in (r["wires"] or []) for r in intervening))
            mechanism = {"matched": matched, "second_minus_first_plus_rto_ns": early,
                         "second_minus_sack_plus_rto_ns": later, "sack_repairs": wires,
                         "classification": ("original_deadline" if abs(early) <= 1_000_000 else
                                            "head_repair_deadline" if abs(later) <= 1_000_000 else
                                            "other_deadline") if matched else "schedule_not_matched"}
        raw = (d / "log").read_text(errors="replace")
        diag = [line.strip()[:520] for line in (raw+"\n"+log).splitlines()
                if re.search(r"^ERROR:|^Error:|^panic:|tcp_rack_test.go:[0-9]+:", line)
                and not re.search(r"token|authorization|api[_-]?key|bearer|header|password|secret", line, re.I)]
        targets[label] = dict(passed=passed, action=status, summary=summary, xml=len(cases), skips=sum("skipped" in v for v in cases.values()), trace=trace, mechanism=mechanism, diagnostics=diag[-15:])
    arms[name] = {"complete": code == 0 and bool(targets) and all(x["passed"] for x in targets.values()), "source": (d / "head").read_text().strip(), "targets": targets, "exit": code, "uuid": (d / "uuid").read_text().strip()}
v = {"complete": all(a["complete"] for a in arms.values()), "arms": arms}
if v["complete"]:
    outcomes = [next(iter(arms[name]["targets"].values()))["mechanism"]["classification"] for name in ("stock","draft")]
    v["comparison"] = "distinct_head_repair_deadline" if outcomes == ["original_deadline","head_repair_deadline"] else "other_observation"
    v["observed"] = outcomes
(root / "records/verdict.json").write_text(json.dumps(v, indent=2)+"\n")
raise SystemExit(not v["complete"])
