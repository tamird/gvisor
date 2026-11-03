"""Classify a reconnect from its originating TCP endpoint and its forwarder."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

root, source, fixture, code = sys.argv[1:]
root = Path(root)
label = "//pkg/tcpip/transport/tcp/test/e2e:forwarder_test"
cases = {
    "TestForwarderReopenAfterTimeWait",
    "TestForwarderTimeWaitSynAckKeepsEndpoint",
    "TestForwarderTimeWaitDeclinedSynGetsReset",
    "TestQueuedForwarderReconnect",
    "TestQueuedForwarderReconnect/single",
    "TestQueuedForwarderReconnect/duplicated",
}
events = [json.loads(line) for line in (root/"events").open()] if (root/"events").is_file() else []
test_log = (root/"test.log").read_text() if (root/"test.log").is_file() else ""
rows = []
for event in events:
    if result := event.get("testResult"):
        info = result.get("executionInfo", {})
        rows.append([event["id"]["testResult"]["label"], result.get("status"), info.get("strategy"),
                     any((info.get("cachedRemotely"),result.get("cachedRemotely"),result.get("cachedLocally")))])
summary = [(e["id"]["testSummary"]["label"], e["testSummary"]["overallStatus"]) for e in events if "testSummary" in e]
ends = [e["finished"] for e in events if "finished" in e]
invocations = [e["started"]["uuid"] for e in events if "started" in e]
passed = re.findall(r"--- PASS: (Test\S+)", test_log)
unsuccessful = re.findall(r"--- (FAIL|SKIP): (Test\S+)", test_log)
observed = re.findall(r"RECONNECT copies=([12]) first=(\d+) client=(\w+) peer_reply=(\d+) forwarder_error=(\S+)",test_log)
single = ("1","18","connected","16","<nil>")
normal = ("2",*single[1:])
refused = ("2","20","refused","4","*tcpip.ErrConnectionRefused")
verdict = "INCOMPLETE"
if (int(code) == 0 and len(ends) == len(invocations) == 1
    and ends[0]["exitCode"].get("code",0) == 0
    and rows == [[label,"PASSED","remote",False]] and summary == [(label,"PASSED")]
    and sorted(passed) == sorted(cases) and not unsuccessful):
    if observed == [single, refused]:
        verdict = "DUPLICATE_REFUSED"
    elif observed == [single, normal]:
        verdict = "BOTH_CONNECTED"
record = dict(product=source,fixture=fixture,verdict=verdict,bazel_exit=int(code),
              invocations=invocations,attempts=rows,summaries=summary,passed=passed,unsuccessful=unsuccessful,
              observed=observed,finished=ends[-1].get("finishTime") if ends else None,
              verdict_at=datetime.now(timezone.utc).isoformat())
(root/"result.json").write_text(json.dumps(record,indent=2)+"\n")
print(json.dumps(record))
sys.exit(verdict == "INCOMPLETE")
