"""Record the original Linux receive result and matched Bazel test outcomes."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

ARM = sys.argv[1]
SOURCES = {"stock": "a1f66b4e411ee96b682fc80fbe177257bde82a63", "fixed": "3abd433515437ea263321eeead064993e3b842b9"}
WORK = Path(os.environ["RUNNER_TEMP"]) / "netgofer-paired"
FDS = "//pkg/tcpip/link/fdbased:fdbased_test"
NET = "//runsc/gofer/net:netgofer_test"
CASES = {
    "TestRecvMMsgPeerShutdown", "TestRecvMMsgKernelShutdown", "TestDispatchPacketFormat",
    "TestSpliceAndCloseWrite", "TestRunProxyModeEndToEnd", "TestRunProxyModeWithSeccomp",
    "TestProxyLimitsConnections", "TestProxyRejectsDisallowedDestinations",
    "TestNetgoferEarlyValidationClosesUDSFD",
}
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()==SOURCES[ARM]
result={"source": SOURCES[ARM], "tests": {}, "exit": int((WORK / (ARM+".exit")).read_text())}
for line in (WORK/(ARM+".jsonl")).open():
    item=json.loads(line)
    if "started" in item:
        result["invocation"]=str(uuid.UUID(item["started"]["uuid"]))
    if "testResult" not in item:
        continue
    label=item["id"]["testResult"]["label"]
    assert label in ({FDS} if ARM=="stock" else {FDS,NET})
    e=item["testResult"]
    info=e.get("executionInfo",{})
    row={"passed": e["status"]=="PASSED", "status": e["status"] if e["status"] in {"PASSED","FAILED","TIMEOUT","INCOMPLETE","FLAKY"} else "other",
         "remote":info.get("strategy")=="remote","cached":bool(e.get("cachedLocally") or info.get("cachedRemotely")),
         "cases":{}}
    result["tests"][label]=row
    p=Path("bazel-testlogs")/label[2:].replace(":","/")/"test.log"
    if p.is_file():
        raw=p.read_bytes()
        row["log_sha256"]=hashlib.sha256(raw).hexdigest()
        data=raw.decode(errors="replace")
        for name in sorted(CASES):
            count = {k:len(re.findall(r"(?m)^\s*"+prefix+name+r"(?:\s|$)",data)) for k,prefix in (
                ("started",r"=== RUN\s+"),("passed",r"--- PASS: "),("failed",r"--- FAIL: "),("skipped",r"--- SKIP: "))}
            if any(count.values()):
                row["cases"][name]=count
        row["nonterminal_after_shutdown"]="dispatch after peer shutdown = (true, <nil>), want (false, nil)" in data
        row["empty_records"]=[int(n) for n in re.findall(r"closed-peer recvmmsg returned (\d{1,2}) records of zero bytes",data)]
        row["signals"]=[msg for msg in ("timed out", "Permission denied", "Operation not permitted","SIGSYS","seccomp child process failed",
                      "want the final frame","want to keep receiving","panic:", "signal: killed") if msg in data]
(WORK/(ARM+".json")).write_text(json.dumps(result,indent=2)+"\n")
