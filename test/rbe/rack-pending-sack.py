"""Select the head policy on the measured integration, or summarize actual packet records."""
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

if sys.argv[1] == "select":
    # Apply #16624's exact replacement to this integration, without
    # modifying its partial-write and original/repair bookkeeping.
    import subprocess
    p = Path("pkg/tcpip/transport/tcp/snd.go")
    producer = subprocess.check_output(["git", "show", "9df20cfc5:" + str(p)], text=True)
    fix_start = producer.index("\tif seg.payloadSize() != 0 {\n\t\tswitch {\n\t\tcase err != nil:")
    fix_end = producer.index("\n\t}", fix_start) + 3
    before = p.read_text()
    begin = before.index("\tif err != nil && seg.payloadSize() != 0 {\n\t\tif (s.FastRecovery.Active && seg.xmitCount")
    end = before.index("\n\t}", begin) + 3
    p.write_text(before[:begin] + producer[fix_start:fix_end] + before[end:])
    sys.exit()

root = Path(os.environ["RUNNER_TEMP"])
result = {}
names = {"ack-arrives", "ack-withheld"}
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
            if n.startswith("TestRACKOldestDeadline/"):
                cases[n.split("/")[-1]] = [x.tag for x in entry if x.tag in {"failure", "error", "skipped"}]
    except ET.ParseError:
        pass
    logs = (load("test.log")+"\n"+load("buildlog")).splitlines()
    diag = list(dict.fromkeys(l.strip()[:480] for l in logs if re.search(r"tcp_rack_test\.go:[0-9]+|^ERROR:|^panic:|^FAIL:|^--- FAIL",l) and not re.search(r"authorization|api[_-]?key|bearer|remote[_-]?header|password|token",l,re.I)))
    traces = [l for l in diag if " DEADLINE " in l]
    code = load("exit").strip()
    ok = code == "0" and set(actions) == set(summaries) == {"PASSED"} and set(cases) == names and not any(cases.values()) and len(traces) == len(names)
    for n in names:
        lines = [l for l in traces if f"DEADLINE {n} " in l]
        if len(lines) != 1:
            ok = False
            continue
        old = 1 if arm == "measured" else 0
        full = int(arm == "measured" or n == "ack-withheld")
        ok &= f"old={old}:" in lines[0] and f"full={full}:" in lines[0]
    result[arm] = dict(verified=ok, source=os.environ["MEASURED"], selector=load("selector").splitlines(), code=code, actions=actions, summaries=summaries, cases=cases, traces=traces, diagnostics=diag[-24:] if not ok else [], invocations=re.findall(r"Invocation ID: ([\w-]+)",load("buildlog")))
record = dict(complete=all(r["verified"] for r in result.values()), arms=result, fixture=hashlib.sha256((root/"rack-pending-sack.go.txt").read_bytes()).hexdigest())
(root/"records"/"verdict.json").write_text(json.dumps(record,indent=2)+"\n")
raise SystemExit(not record["complete"])
