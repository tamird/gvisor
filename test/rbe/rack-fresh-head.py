"""Check named packet executions and preserve distinct baseline/corrected observations."""
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

root = Path(os.environ["RUNNER_TEMP"])
out = {}
arms = ("permanent_linux", "permanent_fixed", "permanent_stock", "late_fixed", "late_mutant", "netstack", "rack", "owners", "analyzer")
for arm in arms:
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
        dest = d / label.removeprefix("//").replace("/", "_").replace(":", "_")
        text = (dest / "test.log").read_text(errors="replace")
        cases = {}
        try:
            for case in ET.parse(dest / "test.xml").iter("testcase"):
                cases[case.get("name", "")] = [c.tag for c in case if c.tag in ("failure", "error", "skipped")]
        except (OSError, ET.ParseError):
            pass
        passed = action == summary == ["PASSED"]
        names = []
        if arm in ("linux", "stock", "netstack"):
            names.extend("TestLimitedTransmitRecovery/" + case for case in ("small", "threshold"))
        if arm in ("netstack", "rack"):
            names.extend("TestSACKCreditBeforeCumulativeACK/" + case for case in ("open", "closed"))
        if arm.startswith(("permanent_", "late_")):
            names.append("TestLimitedTransmit")
        named = {k: v for k, v in cases.items() if any(k.endswith(name) for name in names)}
        passed &= all(any(k.endswith(name) and not v for k,v in named.items()) for name in names)
        trace = [
            s.strip()[:560] for s in text.splitlines()
            if any(marker in s for marker in (
                "CREDIT mode=", "LIMIT mode=", "LATE withheld until cumulative ACK",
                "fourth duplicate ACK sent packet", "first duplicate ACK sent both queued packets",
            )) or re.search(r"tcp_retransmits_test.go:\d+: packet \d+:", s)
        ]
        if arm in ("permanent_stock", "late_mutant"):
            expected = r"packet 10:" if arm == "permanent_stock" else "fourth duplicate ACK sent packet 10 queued after the third"
            passed = code == 3 and action == summary == ["FAILED"] and bool(re.search(expected, text)) and any("failure" in v for v in named.values())
        if arm == "late_fixed":
            passed &= any("LATE withheld until cumulative ACK" in line for line in trace)
        for case in names:
            if "/" not in case:
                continue
            scenario = case.split("/")[1]
            measured = [s for s in trace if "mode=" + scenario + " " in s]
            passed &= len(measured) == 1
            if not measured:
                continue
            line = measured[0]
            if scenario in ("open", "closed"):
                m = re.search(r"first=(\d+).*idle=\[(.*?)\] sack=\[(.*?)\] repeat=\[(.*?)\] extend=\[(.*?)\]",line)
                passed &= bool(m and not m[2] and not m[4] and (m[3],m[5]) == ((m[1],str(int(m[1])+1)) if scenario=="open" else ("","")))
                continue
            m = re.search(r"cwnd=(\d+).*dupacks=(\d+) extra=\[(.*?)\] windows=\[(.*?)\] state=(\d+) ssthresh=(\d+) repaired_us=(\d+) rto_us=(\d+) at_repair=(\d+) completed=(\d+)",line)
            if not m:
                passed = False
                continue
            n,dup,state,ss,elapsed,rto,at,done = map(int,(m[1],m[2],m[5],m[6],m[7],m[8],m[9],m[10]))
            passed &= done == n+2
            if scenario == "small" and arm == "stock":
                passed &= at==n and dup<3 and state==4 and elapsed>=rto*0.8
            elif arm=="netstack" or (arm=="stock" and scenario=="threshold"):
                passed &= dup==3 and state==3 and elapsed<rto and ss==max(2,n//2)
                if arm=="netstack":
                    passed &= m[3].split()[:2] == [str(n),str(n+1)] and m[4].split()[:2]==[str(n),str(n)]
            # The native observation is a control, not an assertion that Linux
            # implements the same controller state accounting as gVisor.
        errors = [name for name, tags in cases.items() if "error" in tags or "failure" in tags]
        platforms = [arch for arch in ("amd64", "arm64") if arch+"..." in text]
        if arm=="owners":
            checks = ["TestSACKRecovery"] if label.endswith(":tcp_sack_test") else [
                "TestCubicWindowGrowthAfterPacketLoss/fast_retransmit/segments_per_ack=" + str(n)
                for n in (1, 2, 10)
            ]
            named.update({k:v for k,v in cases.items() if any(k.endswith(name) for name in checks)})
            passed &= not errors and all(any(k.endswith(name) and not v for k,v in named.items()) for name in checks)
        if arm=="analyzer":
            passed &= bool(cases) and not errors and platforms == ["amd64", "arm64"]
        note = [s.strip()[:520] for s in (text + "\n" + log).splitlines() if re.search(r"SETUP mode=|CREDIT mode=|LIMIT mode=|unexpected|no repair|: error:|fatal error:|command not found|INFO: Elapsed time|tcp_rack_test.go:\d+|^ERROR:|^panic:|^--- FAIL|^FAIL:|^--- SKIP|failed to|disable RACK|check RACK|command returned|operation not permitted|read-only file system", s) and not re.search("token|authorization|api[_-]?key|bearer|remote[_-]?header|password",s,re.I)]
        results[label] = {"passed":passed, "actual":sorted(actual), "action":action, "summary":summary, "xml":len(cases), "skips":sum("skipped" in v for v in cases.values()), "errors":errors, "named":named, "analyzed":platforms, "observations":trace, "diagnostics":note[-24:] if not passed or arm=="stock" else []}
    exit_ok = code == (3 if arm in ("permanent_stock", "late_mutant") else 0)
    out[arm] = {"passed": exit_ok and all(r["passed"] for r in results.values()), "source":(d/"head").read_text().strip(), "exit":code, "targets":results, "invocations":re.findall(r"Invocation ID: ([\w-]+)", log)}
record = {"fixture":hashlib.sha256((root/"rack-fresh-head.go.txt").read_bytes()).hexdigest(), "arms":out, "complete":all(r["passed"] for r in out.values())}
(root/"records/verdict.json").write_text(json.dumps(record,indent=2)+"\n")
raise SystemExit(not record["complete"])
