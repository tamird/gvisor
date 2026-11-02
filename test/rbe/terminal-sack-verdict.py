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
        # The named target, both modes and both independent source completions
        # are required; a numerical contrast is an observation, not a PASS gate.
        target="TestRepeatedTerminalSACK"
        expected={target+"/no_loss",target+"/held_tail"}
        passed=(status==["PASSED"] and summary==["PASSED"] and
                expected.issubset(cases) and set(cases)<=expected|{target} and not any(cases.values()))
        trace=[json.loads(line.split("REPLAY ",1)[1]) for line in log.splitlines() if "REPLAY {" in line]
        groups=[]
        for r in trace:
            if r["phase"]=="original":
                groups.append([])
            if groups:
                groups[-1].append(r)
        metrics={}
        def phase(g,name):
            return next((r for r in g if r["phase"]==name),{})
        for g in groups:
            held=bool(phase(g,"terminal_sack"))
            mode="held_tail" if held else "no_loss"
            first=phase(g,"first_rto")
            first_ack=phase(g,"first_repair_ack")
            end=phase(g,"end")
            original=phase(g,"original")
            passed &= original.get("wire")==list(range(10)) and original.get("timeouts")==0 and bool(end)
            if not end: continue
            if not held:
                passed &= phase(g,"ordinary_ack").get("ack_bytes")==14240 and end["ack_bytes"]==22784 and end["timeouts"]==0
                metrics[mode]={"delivered":end["ack_bytes"],"timeouts":end["timeouts"]}
                continue
            before=phase(g,"terminal_sack")
            geometry=[12816,14240]
            passed &= (before.get("ack_bytes")==0 and before.get("sack_bytes")==geometry and
                       first.get("wire")==[0] and first.get("timeouts")==1 and first.get("state")==1 and
                       first_ack.get("ack_bytes")==1424 and first_ack.get("sack_bytes")==geometry)
            if not first or not first_ack: continue
            following=[r for r in g if r["phase"]=="repair_or_new_ack"]
            passed &= all(r.get("sack_bytes")==geometry for r in following if r["ack_bytes"]<12816)
            pre_second=[r for r in following if r["timeouts"]==1]
            expired=[r for r in following if r["timeouts"]>1]
            payload=[r for r in following if any(i>=10 for i in r["wire"])]
            metrics[mode]={"terminal_sack":geometry,"first_rto_ns":first["ns"],
               "first_rto_interval_ns":first["rto_ns"],"first_ack_ns":first_ack["ns"],"state_after_first_ack":first_ack["state"],
               "ack_bytes_before_second_rto":max([first_ack["ack_bytes"]]+[r["ack_bytes"] for r in pre_second]),
               "later_rtos":[(r["ns"],r["rto_ns"],r["wire"]) for r in g if r["phase"]=="later_rto"],
               "first_ack_after_second_rto_ns":expired[0]["ns"] if expired else None,
               "first_new_data_ns":payload[0]["ns"] if payload else None,"delivered":end["ack_bytes"],"timeouts":end["timeouts"],"end_ns":end["ns"]}
        passed &= set(metrics)=={"no_loss","held_tail"} and len(groups)==2
        mechanism=metrics
        raw = (d / "log").read_text(errors="replace")
        diag = [line.strip()[:520] for line in (raw+"\n"+log).splitlines()
                if re.search(r"^ERROR:|^Error:|^panic:|tcp_rack_test.go:[0-9]+:", line)
                and not re.search(r"token|authorization|api[_-]?key|bearer|header|password|secret", line, re.I)]
        targets[label] = dict(passed=passed, action=status, summary=summary, xml=len(cases), skips=sum("skipped" in v for v in cases.values()), trace=trace, mechanism=mechanism, diagnostics=diag[-15:])
    arms[name] = {"complete": code == 0 and bool(targets) and all(x["passed"] for x in targets.values()), "source": (d / "head").read_text().strip(), "targets": targets, "exit": code, "uuid": (d / "uuid").read_text().strip()}
v = {"complete": all(a["complete"] for a in arms.values()), "arms": arms}
if v["complete"]:
    outcomes=[next(iter(arms[a]["targets"].values()))["mechanism"]["held_tail"] for a in ("stock","draft")]
    stock,guard=outcomes
    v["comparison"]="matched_guard_releases_prior_flight" if (
        stock["first_rto_ns"]==guard["first_rto_ns"] and stock["first_ack_ns"]==guard["first_ack_ns"]
        and stock["state_after_first_ack"]==3 and guard["state_after_first_ack"]==1 and
        stock["ack_bytes_before_second_rto"]<14240 and guard["ack_bytes_before_second_rto"]==22784
        and stock["timeouts"]>=2 and guard["timeouts"]==1) else "other_observation"
(root / "records/verdict.json").write_text(json.dumps(v, indent=2)+"\n")
raise SystemExit(not v["complete"])
