"""Replay genuine loss before and after the Eifel response guards."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

root = Path(os.environ["RUNNER_TEMP"]) / "eifel-check"
owner = "//pkg/tcpip/transport/tcp/test/e2e:tcp_sack_test"
source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert source == "83c64f1f96d23974b51efcc75dedf9661ef40c06"
combined = "cf5569cbd09f643696444f83cffd4dde753e627d"
author = "7e3d55e35d58bfc8c63bdf5582b36e50bc5824dc"
receipt = {"source":source, "author":author, "combined":combined, "other":"1b9768b199524246fe8e4fa66a0b623069778780", "owner":owner}
flags = [
    "--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--local_resources=memory=8192",
    "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
    "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
    "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
    "--strategy=TestRunner=linux-sandbox", "--sandbox_default_allow_network=false",
    "--nocache_test_results", "--test_sharding_strategy=disabled", "--flaky_test_attempts=1",
    "--test_timeout=180", "--test_output=errors", "--color=no", "--curses=no", "--noshow_progress",
]
cases = ["TestEifelFiniteApplicationFlight", "TestEifelGenuineLossAfterSpuriousDetection", "TestTailLossProbeResponseIsKept"]
flags.extend(["--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v",
              "--test_filter=^(" + "|".join(cases) + ")$"])
def check(name):
    bep = root / (name + ".bep")
    cmd = [root / "bazelisk", "--nosystem_rc", "--nohome_rc", "test", *flags,
           "--build_event_json_file=" + str(bep), owner]
    with (root / (name + ".console")).open("wb") as console:
        code = subprocess.run(cmd, stdout=console, stderr=subprocess.STDOUT).returncode
    records = []
    joined = ""
    if bep.is_file():
        for line in bep.open():
            event = json.loads(line)
            if event["id"].get("testResult", {}).get("label") != owner:
                continue
            result = event.get("testResult", {})
            outputs = [f for f in result.get("testActionOutput", []) if f["name"] == "test.log"]
            if not outputs:
                records.append({"aborted": event.get("aborted", {}).get("reason"), "status": result.get("status")})
                continue
            entry, = outputs
            uri = urlsplit(entry["uri"])
            assert uri.scheme == "file"
            data = Path(unquote(uri.path)).read_bytes()
            joined += data.decode(errors="replace")
            records.append({"status": result.get("status"), "cached": result.get("cachedLocally", False),
                            "strategy": result.get("executionInfo", {}).get("strategy"),
                            "log_sha256": hashlib.sha256(data).hexdigest()})
    statuses = {name: status for status, name in re.findall(r'^\s*--- (PASS|FAIL|SKIP): ([A-Za-z0-9_/]+) \(', joined, re.MULTILINE)}
    threshold = re.findall(r'ssthresh after [^\n=]+ = ([0-9]+), want ([0-9]+)', joined)
    detail = {"exit": code, "records": records, "cases": statuses, "threshold_mismatch": threshold}
    detail["two_timeouts"] = re.findall(r"two RTOs before detection, original=(true|false): ssthresh=(\d+)", joined)
    detail["final"] = {kind: {"state": int(state), "cwnd": int(cwnd), "ssthresh": int(threshold), "timeouts": int(rtos)} for kind,state,cwnd,threshold,rtos in re.findall(r"FINAL_EIFEL kind=(\w+) initial_state=(\d+) cwnd=(\d+) ssthresh=(\d+) rtos=(\d+)", joined)}
    receipt[name] = detail
    (root / "receipts.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(name, detail, flush=True)
    return detail

def git(*args):
    return subprocess.check_output(["git", *args])


test = Path("pkg/tcpip/transport/tcp/test/e2e/tcp_sack_test.go")
rack = Path("pkg/tcpip/transport/tcp/rack.go")
assert git("show", author + ":" + str(test)) == git("show", combined + ":" + str(test))
fixture = test.read_text()
start = fixture.index("func TestEifelGenuineLossAfterSpuriousDetection(")
end = fixture.index("// TestTailLossProbeResponseIsKept", start)
case = fixture[start:end]
assert case.count("stats := c.Stack().Stats().TCP") == 1 and case.count("state := ack(len(data), repair)") == 1
case = case.replace("stats := c.Stack().Stats().TCP", "detectedState := info().CcState\n\t\t\tstats := c.Stack().Stats().TCP")
case = case.replace("state := ack(len(data), repair)", 'state := ack(len(data), repair)\n\t\t\tt.Logf("FINAL_EIFEL kind=%s initial_state=%d cwnd=%d ssthresh=%d rtos=%d", scenario, detectedState, state.SndCwnd, state.Ssthresh, stats.Timeouts.Value())')
fixture = fixture[:start] + case + fixture[end:]
receipt["instrumented_test_sha256"] = hashlib.sha256(fixture.encode()).hexdigest()
delta = git("diff", combined, source, "--", str(rack))
receipt["loss_delta_sha256"] = hashlib.sha256(delta).hexdigest()
for arm, head, patch in (("combined_original", combined, False), ("combined_fixed", combined, True), ("standalone_fixed", author, True)):
    subprocess.run(["git","checkout","--quiet","--detach",head],check=True)
    test.write_text(fixture)
    if patch:
        subprocess.run(["git","apply","-"], input=delta,check=True)
    receipt[arm + "_rack_blob"] = git("hash-object",str(rack)).decode().strip()
    try:
        check(arm)
    finally:
        subprocess.run(["git","restore","--worktree","--source="+head,"--",str(rack),str(test)],check=True)
wanted = {"TestEifelGenuineLossAfterSpuriousDetection/rack", "TestEifelGenuineLossAfterSpuriousDetection/timeout",
          "TestEifelFiniteApplicationFlight/original_ack", "TestEifelFiniteApplicationFlight/repair_ack", "TestTailLossProbeResponseIsKept"}
def qualified(name, fixed):
    row = receipt[name]
    cases = row["cases"]
    return (len(row["records"]) == 1 and not row["records"][0]["cached"] and row["records"][0]["strategy"]=="linux-sandbox"
            and wanted <= cases.keys() and all(cases[n]==("FAIL" if n.endswith("/rack") and not fixed else "PASS") for n in wanted)
            and row["exit"]==(0 if fixed else 3) and row["final"]["timeout"]["ssthresh"]==2
            and (row["final"]["rack"]["ssthresh"]==2) == fixed)
receipt["verified"] = all(qualified(name,fix) for name,fix in (("combined_original",False),("combined_fixed",True),("standalone_fixed",True)))
(root/"receipts.json").write_text(json.dumps(receipt,indent=2)+"\n")
raise SystemExit(not receipt["verified"])
