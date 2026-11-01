"""Select the head policy on the measured integration, or summarize actual packet records."""
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

if sys.argv[1] == "select":
    # Keep every other change on the WAN source, including timer handling.
    for name, receiver, indent in (("snd.go", "s", "\t\t\t"), ("rack.go", "rc.snd", "\t\t")):
        p = Path("pkg/tcpip/transport/tcp") / name
        before = p.read_text()
        signature = f"{receiver}.cc.HandleLossDetected()\n{indent}{receiver}.enterRecovery()\n{indent}fastRetransmit = true"
        if before.count(signature) != 1:
            raise SystemExit(f"unexpected RACK entry in {p}")
        p.write_text(before.replace(signature, signature.removesuffix("true") + f"{receiver}.writeList.Front().lost"))
    sys.exit()

root = Path(os.environ["RUNNER_TEMP"])
result = {}
names = {"time-only", "hold-repeat", "hold-extend", "process-extend"}
label = "//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test"
for arm in ("measured", "select"):
    d = root / "records" / arm
    def load(name):
        p = d / name
        return p.read_text(errors="replace") if p.is_file() else ""
    bep = root / (arm + ".bep")
    wanted = {label}
    actions, summaries = [], []
    if bep.is_file():
        with bep.open() as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e["id"].get("targetCompleted", {}).get("label") in wanted:
                    for c in e.get("children", []):
                        for key in ("targetCompleted", "testSummary", "testResult"):
                            if "label" in c.get(key, {}):
                                wanted.add(c[key]["label"])
                if e["id"].get("testResult", {}).get("label") in wanted:
                    actions.append(e["testResult"].get("status"))
                if e["id"].get("testSummary", {}).get("label") in wanted:
                    summaries.append(e["testSummary"].get("overallStatus"))
    cases = {}
    try:
        for entry in ET.fromstring(load("test.xml")).iter("testcase"):
            n = entry.get("name", "")
            if n.startswith("TestRACKPendingSACK/"):
                cases[n.split("/")[-1]] = [x.tag for x in entry if x.tag in {"failure", "error", "skipped"}]
    except ET.ParseError:
        pass
    logs = (load("test.log")+"\n"+load("buildlog")).splitlines()
    diag = list(dict.fromkeys(l.strip()[:480] for l in logs if re.search(r"tcp_rack_test\.go:[0-9]+|^ERROR:|^panic:|^FAIL:|^--- FAIL",l) and not re.search(r"authorization|api[_-]?key|bearer|remote[_-]?header|password|token",l,re.I)))
    traces = [l for l in diag if " ORDER " in l]
    code = load("exit").strip()
    ok = code == "0" and set(actions) == set(summaries) == {"PASSED"} and set(cases) == names and not any(cases.values()) and len(traces) == len(names)
    result[arm] = dict(verified=ok, source=os.environ["MEASURED"], selector=load("selector").splitlines(), code=code, actions=actions, summaries=summaries, cases=cases, traces=traces, diagnostics=diag[-24:] if not ok else [], invocations=re.findall(r"Invocation ID: ([\w-]+)",load("buildlog")))
record = dict(complete=all(r["verified"] for r in result.values()), arms=result, fixture=hashlib.sha256((root/"rack-pending-sack.go.txt").read_bytes()).hexdigest())
(root/"records"/"verdict.json").write_text(json.dumps(record,indent=2)+"\n")
raise SystemExit(not record["complete"])
