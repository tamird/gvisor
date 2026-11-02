"""Read the original per-arm Bazel/packet outcome before comparing deadlines."""

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import sys


OUT = Path(os.environ["RUNNER_TEMP"]) / "review-results"
PLAN = json.loads((OUT / "plan.json").read_bytes())
LABEL = "//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test"
TESTS = {
    "TestRACKTLPNotLaterThanRTO", "TestRACKTLPNotLaterThanOldestRTO",
    "TestRACKTLPOneRTOAfterAck", "TestRACKTLPFlightAfterAck", "TestRACKTLPRecoveryExitWire",
}
CONTEXT = r"EXIT_CONTEXT old_ns=(\d+) fresh_ns=(\d+) armed_ns=(\d+) sent=(\d+) repaired=(\d+) marked=(\d+) cumulative=(\d+) tail=(\d+) out=(\d+) sacked=(\d+)"
def now():
    return datetime.now(timezone.utc).isoformat()

def observe(arm, code, head, started):
    path = OUT / f"{arm}.bep"
    with path.open() as src:
        events = [json.loads(line) for line in src]
    results = [(e["id"]["testResult"]["label"], e["testResult"]) for e in events if "testResult" in e]
    attempts = [(label, r.get("status"), r.get("executionInfo",{}).get("strategy"), bool(r.get("cachedLocally") or r.get("cachedRemotely") or r.get("executionInfo",{}).get("cachedRemotely"))) for label, r in results]
    summaries = [(e["id"]["testSummary"]["label"], e["testSummary"]["overallStatus"]) for e in events if "testSummary" in e]
    inv = [e["started"]["uuid"] for e in events if "started" in e]
    ends = [e["finished"] for e in events if "finished" in e]
    path = OUT / f"{arm}.log"
    log = path.read_text() if path.is_file() else ""
    passed = re.findall(r"--- PASS: (Test\S+)", log)
    failed = re.findall(r"--- (FAIL|SKIP): (Test\S+)", log)
    contexts = [list(map(int,m)) for m in re.findall(CONTEXT, log)]
    tails = [(int(t), int(n), seq, b) for t,n,seq,b in re.findall(r"EXIT_TAIL ns=(\d+) bytes=(\d+) seq=(\d+) payload=([0-9a-f]+)",log)]
    empty = list(map(int,re.findall(r"EXIT_EMPTY ns=(\d+)",log)))
    ok = (head == PLAN[arm]["head"] and len(inv) == 1 and len(ends) == 1
          and ends[0]["exitCode"].get("code",0) == int(code) == 0
          and attempts == [(LABEL,"PASSED","remote",False)] and summaries == [(LABEL,"PASSED")]
          and sorted(passed) == sorted(TESTS) and not failed and len(contexts) == len(tails) == 1)
    if len(contexts) == len(tails) == 1:
        old,fresh,armed,*flight = contexts[0]; sent,size,seq,payload = tails[0]
        ok &= (0 < old < fresh and flight == [40,10,50,50,60,1,0] and size == 10
               and payload == bytes(range(50,60)).hex() and int(seq) < 1<<32
               and (armed == sent == old and empty == [old-1] if arm=="published"
                    else armed == sent == fresh and empty == [old-1,old,fresh-1]))
    diagnostics=[]
    path = Path(os.environ["RUNNER_TEMP"]) / f"{arm}.bazel"
    if path.is_file():
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith(("ERROR:", "FAIL:", "error:")) and not any(k in line.lower() for k in ("credential","token","api-key","remote_header","bes_header")):
                diagnostics.append(line[:400])
                if len(diagnostics)==6: break
    return dict(head=head,product=PLAN[arm]["product"],code=int(code),started=started,finished=ends[-1].get("finishTime") if ends else None,verdict=now(),valid=bool(ok),invocations=inv,attempts=attempts,summaries=summaries,passed=passed,failed=failed,contexts=contexts,tails=tails,empty=empty,build_errors=diagnostics)

if sys.argv[1] == "finish":
    rows = {arm:json.loads((OUT/f"{arm}.json").read_bytes()) for arm in PLAN if (OUT/f"{arm}.json").exists()}
    valid = set(rows)==set(PLAN) and all(r["valid"] for r in rows.values())
    if valid:
        a,b=(rows[k]["contexts"][0] for k in ("published","corrected"))
        valid = a[:2]==b[:2] and a[3:]==b[3:]
    (OUT/"verdict.json").write_text(json.dumps(dict(rows=rows,missing=sorted(PLAN.keys()-rows.keys()),valid=valid,verdict_at=now()),indent=2)+"\n")
    sys.exit(not valid)
arm,code,head,start=sys.argv[1:]
try:
    row=observe(arm,code,head,start)
except Exception as exc:
    row=dict(head=head,code=int(code),valid=False,error=f"{type(exc).__name__}: {exc}"[:300])
(OUT/f"{arm}.json").write_text(json.dumps(row,indent=2)+"\n")
