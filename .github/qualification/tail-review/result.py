import datetime
import json
import os
from pathlib import Path
import re
import sys

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "review-results"
out.mkdir(exist_ok=True)
label = "//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test"
now = lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
if sys.argv[1] == "finish":
    rows = [json.loads((out / f"{arm}.json").read_text()) for arm in ("sack", "probe", "receive")]
    before, after = rows[1]["cwnd"], rows[2]["cwnd"]
    observed = all(row["complete"] for row in rows) and bool(before and after) and before[0] == after[0] and before[1] < before[0] == after[1] and before[2] == after[2] == 0
    (out / "verdict.json").write_text(json.dumps({"source": os.environ["GITHUB_SHA"], "rows": rows, "verified": now(), "observed": observed}, indent=2) + "\n")
    sys.exit(0 if observed else 1)

arm, code, head = sys.argv[1:]
events = [json.loads(v) for v in (tmp / f"{arm}.jsonl").read_text().splitlines() if v]
summary = [v["testSummary"]["overallStatus"] for v in events if v.get("id", {}).get("testSummary", {}).get("label") == label and "testSummary" in v]
log = Path("bazel-testlogs/pkg/tcpip/transport/tcp/test/e2e/tcp_rack_test/test.log")
data = log.read_text() if log.exists() else ""
pass_names = set(re.findall(r"--- PASS: (Test\S+)", data))
skips = re.findall(r"--- SKIP: (Test\S+)", data)
failures = re.findall(r"--- FAIL: (Test\S+)", data)
expected = ({f"TestRACKTLPRecoveryWithTimestamps/{v}" for v in ("whole_probe", "half_of_probe")} | {f"TestRACKSelectiveProbeExtent/sack_{n}_bytes" for n in (5, 10)}) if arm == "sack" else {f"TestRACKTLPLostProbeRTO/{v}" for v in ("no_ACK", "writes", "duplicate_ACK", "ACK_of_unsent_data", "ACK_of_unsent_data_after_retransmitted_probe", "writes_after_retransmitted_probe")}
actual = {v for v in pass_names if "/" in v}
cwnd = re.findall(r"FUTURE_ACK cwnd_before=(\d+) cwnd_after=(\d+) timeouts=(\d+)", data)
flight = re.findall(r"FLIGHT SACK bytes=\[(\d+),(\d+)\) cumulative=(\d+) probe end=(\d+) RACK end=(\d+) timeouts=(\d+)", data)
complete = code == "0" and actual == expected and not failures and not skips and summary == ["PASSED"]
if arm == "sack":
    complete &= sorted(flight) == [("30", "35", "10", "40", "10", "0"), ("30", "40", "10", "40", "40", "0")]
else:
    complete &= len(cwnd) == 1
finished = [v["finished"].get("finishTime") for v in events if "finished" in v]
receipt = {"arm": arm, "source": head, "target": label, "code": int(code), "summaries": summary, "missing": sorted(expected - actual), "unexpected": sorted(actual - expected), "executed": sorted(actual), "skips": skips, "failures": failures, "flight": flight, "cwnd": list(map(int, cwnd[0])) if cwnd else [], "finished": finished, "verified": now(), "complete": complete}
(out / f"{arm}.json").write_text(json.dumps(receipt, indent=2) + "\n")
