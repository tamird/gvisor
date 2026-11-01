"""Reconcile real TestResults and per-case XML; keep partial evidence."""
from pathlib import Path
from xml.etree import ElementTree as ET
import json
import os
import re

root = Path(os.environ["RUNNER_TEMP"])
arms = {}
expected = {"wrap/original", "wrap/repair", "two_timeouts/original", "two_timeouts/repair"}
for name in ("stock", "suites", "analyzers"):
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
        want = "FAILED" if name == "stock" else "PASSED"
        passed = status == [want] and summary == [want] and bool(cases)
        if name != "stock":
            passed &= not any("failure" in v or "error" in v for v in cases.values())
        named = {k.removeprefix("TestEifelRetransmissionTimestamps/"): v for k, v in cases.items()
                 if k.startswith("TestEifelRetransmissionTimestamps/") and k.count("/") == 2}
        if key == "tcp_sack_test":
            passed &= set(named) == expected
            passed &= all((v == ["failure"] if name == "stock" and k.endswith("/original") else not v) for k, v in named.items())
            if name == "suites":
                for old in ("timestamps", "no_timestamps", "missing_timestamp", "zero_echo"):
                    passed &= cases.get("TestDetectSpuriousRecoveryWithRTO/"+old) == []
        marks = [cpu for cpu in ("amd64", "arm64") if cpu in log]
        if key.endswith("_nogo"):
            passed &= len(marks) == 2
        raw = (d / "log").read_text(errors="replace")
        diag = [line.strip()[:520] for line in (log+"\n"+raw).splitlines()
                if re.search(r"^ERROR:|^Error:|^panic:|original [0-9]+, repair [0-9]+|tcp_sack_test.go:[0-9]+:", line)
                and not re.search(r"token|authorization|api[_-]?key|bearer|header|password|secret", line, re.I)]
        targets[label] = dict(passed=passed, action=status, summary=summary, xml=len(cases), skips=sum("skipped" in v for v in cases.values()), named=named, diagnostics=diag[-15:], analyzer_arch=marks if key.endswith("_nogo") else None)
    arms[name] = {"complete": code == (3 if name == "stock" else 0) and bool(targets) and all(x["passed"] for x in targets.values()), "source": (d / "head").read_text().strip(), "targets": targets, "exit": code, "uuid": (d / "uuid").read_text().strip()}
v = {"complete": all(a["complete"] for a in arms.values()), "arms": arms}
(root / "records/verdict.json").write_text(json.dumps(v, indent=2)+"\n")
raise SystemExit(not v["complete"])
