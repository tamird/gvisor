import datetime
import json
import os
from pathlib import Path
import re
import sys

out = Path(os.environ["RUNNER_TEMP"]) / "review-results"
out.mkdir(exist_ok=True)
plan = json.loads((out / "plan.json").read_text())
def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
if sys.argv[1] == "finish":
    rows = {arm: json.loads((out / f"{arm}.json").read_text()) for arm in plan if (out / f"{arm}.json").is_file()}
    missing = sorted(plan.keys() - rows.keys())
    valid = not missing and all(row["valid"] for row in rows.values())
    (out / "verdict.json").write_text(json.dumps(dict(rows=rows, missing=missing, verified=now(), valid=valid), indent=2) + "\n")
    sys.exit(not valid)
arm, code, sha, started = sys.argv[1:]
expected = {"TestRACKTLPTwoToOneWire"}
if arm != "parent":
    expected.update(("TestRACKTLPNotLaterThanRTO", "TestRACKTLPNotLaterThanOldestRTO", "TestRACKTLPOneRTOAfterAck"))
label = "//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test"
events = [json.loads(line) for line in (out / f"{arm}.bep").read_text().splitlines() if line]
summary = [e["testSummary"]["overallStatus"] for e in events if e.get("id", {}).get("testSummary", {}).get("label") == label and "testSummary" in e]
raw = out / f"{arm}.log"
log = raw.read_text() if raw.is_file() else ""
names = set(re.findall(r"--- PASS: (Test\S+)", log))
skips_or_failures = re.findall(r"--- (?:FAIL|SKIP): (Test\S+)", log)
repairs = re.findall(r"PTO_REPAIR elapsed_ns=(\d+) multi_ns=(\d+) rto_ns=(\d+) single_ns=(\d+) offset=(\d+) bytes=(\d+) packet=([a-f0-9]+)", log)
empty = list(map(int, re.findall(r"PTO_WIRE elapsed_ns=(\d+) empty", log)))
complete = code == "0" and summary == ["PASSED"] and names == expected and not skips_or_failures and len(repairs) == 1 and sha == plan[arm]["head"]
wire = None
if len(repairs) == 1:
    elapsed, multi, rto, single, off, size = map(int, repairs[0][:6])
    wire = dict(elapsed_ns=elapsed, multi_ns=multi, rto_ns=rto, single_ns=single, offset=off, size=size, packet=repairs[0][6], empty=empty)
    wanted = plan[arm]["first_probe"]
    complete = complete and 0 < multi < rto < single and elapsed == dict(multi=multi, rto=rto, single=single)[wanted] and empty and empty[-1] == elapsed - 1 and (off, size) == (20, 10)
invocations = [e["started"]["uuid"] for e in events if "started" in e and "uuid" in e["started"]]
row = dict(invocations=invocations, product=plan[arm]["product"], sha=sha, arm=arm, started=started, finished=now(), code=int(code), summary=summary, executed=sorted(names), skips_or_failures=skips_or_failures, wire=wire, valid=bool(complete))
(out / f"{arm}.json").write_text(json.dumps(row, indent=2) + "\n")
print(json.dumps(row))
