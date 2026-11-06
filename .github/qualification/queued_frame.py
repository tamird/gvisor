"""Copy the one pinned test and retain its socket queue counts."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

WORK = Path(os.environ["RUNNER_TEMP"]) / "netgofer-queue"
FILE = Path("pkg/tcpip/link/fdbased/endpoint_test.go")
SOURCES = {"stock": "949d4871550821371649ae873beab69ead0b36c1", "poll": "3abd433515437ea263321eeead064993e3b842b9"}
TEST_SOURCE = "e12ccfeb263a09802c01640b6e92d10ad41b0dd3"
arm, mode = sys.argv[1:3]
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == SOURCES[arm]
source = subprocess.check_output(["git","show",f"{TEST_SOURCE}:{FILE}"],text=True)
start = source.index("// TestRecvMMsgQueuedFrameControl")
end = source.index("func TestDispatchPacketFormat", start)
test = source[start:end]
if mode == "stage":
    old = FILE.read_text()
    assert "func TestRecvMMsgQueuedFrameControl(" not in old
    FILE.write_text(old + "\n" + test)
else:
    result = {"source":SOURCES[arm], "test_sha256": hashlib.sha256(test.encode()).hexdigest(),
              "exit": int(sys.argv[3]), "rows":[]}
    result["failed_actions"] = []
    events = WORK / (arm + ".jsonl")
    if events.is_file():
        for line in events.open():
            e=json.loads(line)
            if "action" in e and not e["action"].get("success") and len(result["failed_actions"]) < 8:
                ident = e["id"].get("actionCompleted", {})
                label = ident.get("label", "")
                mnemonic = e["action"].get("type", "")
                if re.fullmatch(r"[@A-Za-z0-9_./+:~-]{1,280}", label) and re.fullmatch(r"[A-Za-z0-9_]{1,60}", mnemonic):
                    result["failed_actions"].append([label, mnemonic, int(e["action"].get("exitCode", 0))])
            if "testResult" not in e:
                continue
            assert e["id"]["testResult"]["label"]=="//pkg/tcpip/link/fdbased:fdbased_test"
            item=e["testResult"]
            result["passed"]=item["status"]=="PASSED"
            result["cached"]=bool(item.get("cachedLocally") or item.get("executionInfo",{}).get("cachedRemotely"))
            result["strategy"]=item.get("executionInfo",{}).get("strategy") if item.get("executionInfo",{}).get("strategy") in ("linux-sandbox","processwrapper-sandbox","local","standalone") else "other"
    log=Path("bazel-testlogs/pkg/tcpip/link/fdbased/fdbased_test/test.log")
    if log.is_file():
        data=log.read_bytes(); result["log_sha256"]=hashlib.sha256(data).hexdigest()
        for m in re.finditer(rb"prefix=(\d{1,3}) keep=(true|false) before=(\d{1,5}) after=(\d{1,5}) manual=(\d{1,3}) remaining=(\d{1,3})",data):
            result["rows"].append([int(m[1]),m[2]==b"true",*[int(x) for x in m.groups()[2:]]])
    console=(WORK/(arm+".console")).read_text(errors="replace")
    result["diagnostics"]=[s for s in ("No space left on device","Out of memory","no matching toolchains","Permission denied","No such file or directory","exit 33","failed to build") if s.lower() in console.lower()]
    # Bazel receives an empty environment except for the public build tools,
    # HOME, USER and locale. Keep short native failure lines, including the real
    # compiler error, while excluding command invocations and authentication.
    problems=[]
    for line in console.splitlines():
        if len(problems)>=15:
            break
        if re.search(r"(?i)token|authorization|x-buildbuddy|--remote_header|secret|Bearer |https?://[^ ]+@",line):
            continue
        if re.search(r"(?i)^ERROR: |\.go:\d+:|no usable spawn strategy|no useable spawn strategy|sandbox.*not supported",line):
            problems.append(line[:350])
    result["failure_lines"]=problems if not result.get("rows") else []
    (WORK/(arm+".json")).write_text(json.dumps(result,indent=2)+"\n")
