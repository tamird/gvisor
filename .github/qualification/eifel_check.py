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
assert source == "d86a594a42673683d6aa16ec5d4c1debe7341048"
receipt = {"source": source, "owner": owner}
flags = [
    "--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--local_resources=memory=8192",
    "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
    "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
    "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
    "--strategy=TestRunner=linux-sandbox", "--sandbox_default_allow_network=false",
    "--nocache_test_results", "--test_sharding_strategy=disabled", "--flaky_test_attempts=1",
    "--test_timeout=180", "--test_output=errors", "--color=no", "--curses=no", "--noshow_progress",
]
cases = [
    "TestDetectSpuriousRecoveryWithRTO", "TestEifelFiniteApplicationFlight",
    "TestEifelGenuineLossAfterSpuriousDetection", "TestTailLossProbeResponseIsKept",
    "TestEifelRetransmissionTimestamps",
]
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
    receipt[name] = detail
    (root / "receipts.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(name, detail, flush=True)
    return detail

previous = "c02d0940fd57e7c31432c81164f3e58f41795286"
author = "7e3d55e35d58bfc8c63bdf5582b36e50bc5824dc"
paths = ["pkg/tcpip/transport/tcp/" + name for name in ("rack.go", "snd.go")]
test = Path("pkg/tcpip/transport/tcp/test/e2e/tcp_sack_test.go")
delta = subprocess.check_output(["git", "diff", author, previous, "--", *paths])
receipt["previous"] = previous
receipt["author"] = author
receipt["reverse_delta"] = hashlib.sha256(delta).hexdigest()
for kind, head in (("standalone", author), ("stack", source)):
    subprocess.run(["git", "switch", "--quiet", "--detach", head], check=True)
    fixture = test.read_bytes() if kind == "standalone" else (root / "stack-fixture.go").read_bytes()
    test.write_bytes(fixture)
    receipt[kind + "_test_sha256"] = hashlib.sha256(fixture).hexdigest()
    subprocess.run(["git", "apply", "--check"], input=delta, check=True)
    subprocess.run(["git", "apply"], input=delta, check=True)
    if kind == "standalone":
        for path in paths:
            assert Path(path).read_bytes() == subprocess.check_output(["git", "show", previous + ":" + path])
    check(kind + "_before")
    subprocess.run(["git", "restore", "--source=" + head, "--worktree", "--", *paths], check=True)
    assert test.read_bytes() == fixture
    check(kind + "_after")
    subprocess.run(["git", "restore", "--source=" + head, "--worktree", "--", str(test)], check=True)

tail = "TestTailLossProbeResponseIsKept"
loss = "TestEifelGenuineLossAfterSpuriousDetection"
for arm in ("standalone", "stack"):
    data = receipt[arm + "_after"]
    expected = {tail, loss + "/timeout", loss + "/rack"}
    expected |= {"TestEifelFiniteApplicationFlight/" + ack for ack in ("original_ack", "repair_ack")}
    expected |= {"TestDetectSpuriousRecoveryWithRTO/" + echo for echo in ("timestamps", "no_timestamps", "missing_timestamp", "zero_echo")}
    if arm == "stack":
        expected |= {"TestEifelRetransmissionTimestamps/" + mode + "/" + ack for mode in ("wrap", "two_timeouts") for ack in ("original", "repair")}
        assert set(map(tuple, data["two_timeouts"])) == {("true", "4294967295"), ("false", "2")}
    assert all(data["cases"].get(name) == "PASS" for name in expected)
    assert data["exit"] == 0 and all(value == "PASS" for value in data["cases"].values())
    assert receipt[arm + "_before"]["cases"].get(tail) == "FAIL"
    for suffix in ("before", "after"):
        [record] = receipt[arm + "_" + suffix]["records"]
        assert not record["cached"] and record["strategy"] == "linux-sandbox"
for mode in ("rack", "timeout"):
    assert receipt["stack_before"]["cases"].get(loss + "/" + mode) == "FAIL"
