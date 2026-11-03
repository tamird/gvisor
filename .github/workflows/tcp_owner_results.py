"""Run the three real dispatcher variants and retain each original outcome."""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = "471d6dff5229b99208bf6fec985601067f1f9d69"
FIX = "ef8ab462943c70e5c52cc57de4b60213f7979fa1"
SOURCE = Path("pkg/tcpip/transport/tcp/dispatcher.go")
FLAGS = ["-c", "opt", "--config=x86_64", "--jobs=4"]
TARGET = "//pkg/tcpip/transport/tcp:tcp_test"
TEMP = Path(os.environ["RUNNER_TEMP"])
OUT = TEMP / "owner"
OUT.mkdir(exist_ok=True)


def git(*args):
    return subprocess.check_output(["git", *args], text=True).rstrip("\n")


def once(source, old, new):
    if source.count(old) != 1:
        raise ValueError("source no longer matches the observation point")
    return source.replace(old, new)


def prepare(policy):
    source = git("show", f"{BASE if policy == 'stock' else FIX}:{SOURCE}") + "\n"
    if policy == "first":
        source = once(source, "if firstContended == ep {", "if !acquired {")
    source = once(source, "type processor struct {", 'type processor struct {\n\tresearchRequeue func(*Endpoint) `state:"nosave"`')
    anchor = "\n\t\t\t\t\tp.epQ.enqueue(ep)\n"
    source = once(source, anchor, anchor + "\t\t\t\t\tif p.researchRequeue != nil { p.researchRequeue(ep) }\n")
    SOURCE.write_text(source)
    return hashlib.sha256(source.encode()).hexdigest()


def observe(policy):
    source_digest = prepare(policy)
    bep = TEMP / f"{policy}.bep"
    started = time.monotonic()
    status = subprocess.run([
        "bazel", "test", *FLAGS, "--strategy=TestRunner=sandboxed", "--nocache_test_results",
        "--test_filter=^TestProcessorContendedPass$", "--test_arg=-test.v",
        "--test_env=GODEBUG=asyncpreemptoff=0", "--test_timeout=30",
        f"--build_event_json_file={bep}", TARGET,
    ]).returncode
    duration = time.monotonic() - started
    log = Path("bazel-testlogs/pkg/tcpip/transport/tcp/tcp_test/test.log")
    text = log.read_text() if log.exists() else ""
    attempts = []
    if bep.exists():
        for line in bep.read_text().splitlines():
            e = json.loads(line)
            if e["id"].get("testResult", {}).get("label") == TARGET:
                t = e["testResult"]
                info = t.get("executionInfo", {})
                attempts.append(dict(status=t["status"], cached=t.get("cachedLocally", False) or info.get("cachedRemotely", False),
                                     strategy=info.get("strategy"), start=t.get("testAttemptStart"), duration=t.get("testAttemptDuration")))
    rows = []
    for payload in re.findall(r"PASS_ROW ([^\r\n]+)", text):
        row = dict(item.split("=", 1) for item in payload.split())
        for k in ("i","ns","miss","other","backlog","batches","peer_resume","peer_release","late_resume","late_release","a","b","peer","late"):
            row[k] = int(row[k])
        rows.append(row)
    expected = [(i,k) for i in range(12) for k in ("backlog","arrival")]
    gates = dict(native_exit=status == 0, fresh=len(attempts) == 1 and not attempts[0]["cached"] and attempts[0]["status"] == "PASSED" and attempts[0]["strategy"] == "processwrapper-sandbox",
                 runtime=text.count("PASS_ENV go=go1.27.3 os=linux arch=amd64 p=1 debug=asyncpreemptoff=0") == 1,
                 named=text.count("--- PASS: TestProcessorContendedPass (") == 1,
                 trials=[(r["i"],r["kind"]) for r in rows] == expected,
                 drained=all(r["a"] == r["b"] == 1 and r["empty"] == "true" and r["capped"] == "false" and r["ns"] > 0 and r["miss"] > 0 for r in rows),
                 workload=all((r["backlog"],r["peer"],r["batches"],r["late"],r["behind"]) ==
                              ((201,201,2,0,"false") if r["kind"]=="backlog" else (0,0,0,1,"true")) for r in rows))
    report=dict(policy=policy, product=BASE if policy=="stock" else FIX, source_sha256=source_digest,
                exit=status, duration_seconds=duration, attempts=attempts, rows=rows, gates=gates,
                observed=datetime.now(timezone.utc).isoformat(), verdict="MEASURED" if all(gates.values()) else "INCOMPLETE")
    (OUT/f"{policy}.json").write_text(json.dumps(report, indent=2)+"\n")
    (OUT/f"{policy}.bench").write_text("".join(f"BenchmarkOwnerResume/{r['kind']} 1 {r['ns']} ns/op {r['miss']} retries/op\n" for r in rows if r["capped"] == "false"))
    print(policy, report["verdict"], gates, flush=True)
    return all(gates.values())


if git("rev-parse", "HEAD^") != FIX or git("rev-parse", f"{FIX}^") != BASE:
    raise ValueError("unverified source")
if sys.argv[1:] == ["measure"]:
    good = [observe(policy) for policy in ("stock","pass","first")]
    raise SystemExit(0 if all(good) else 1)
raise ValueError("unknown mode")
