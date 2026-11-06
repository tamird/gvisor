"""Run the author’s same Eifel assertions before and after the requested timeout change."""
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
assert source == "c02d0940fd57e7c31432c81164f3e58f41795286"
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
    "TestSACKDetectSpuriousRecoveryWithDupACK", "TestNoSpuriousRecoveryWithDSACK",
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
            result = event["testResult"]
            entry, = (f for f in result["testActionOutput"] if f["name"] == "test.log")
            uri = urlsplit(entry["uri"])
            assert uri.scheme == "file"
            data = Path(unquote(uri.path)).read_bytes()
            joined += data.decode(errors="replace")
            records.append({"status": result.get("status"), "cached": result.get("cachedLocally", False),
                            "strategy": result.get("executionInfo", {}).get("strategy"),
                            "log_sha256": hashlib.sha256(data).hexdigest()})
    statuses = {name: status for status, name in re.findall(r'^\s*--- (PASS|FAIL|SKIP): ([A-Za-z0-9_/]+) \(', joined, re.MULTILINE)}
    threshold = re.findall(r'got info\.SndSsthresh = ([0-9]+), want = ([0-9]+)', joined)
    detail = {"exit": code, "records": records, "cases": statuses, "threshold_mismatch": threshold}
    receipt[name] = detail
    (root / "receipts.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(name, detail, flush=True)
    return detail

previous = "d0439e8b83fafeff675910fc544a393fb5d9f85c"
path = "pkg/tcpip/transport/tcp/test/e2e/tcp_sack_test.go"
fixture = Path(path).read_bytes()
receipt["test_file_sha256"] = hashlib.sha256(fixture).hexdigest()
receipt["previous_source"] = previous
subprocess.run(["git", "switch", "--quiet", "--detach", previous], check=True)
Path(path).write_bytes(fixture)
assert subprocess.check_output(["git", "diff", "--name-only"], text=True).splitlines() == [path]
old = check("previous_with_revised_assertion")
subprocess.run(["git", "restore", "--", path], check=True)
subprocess.run(["git", "switch", "--quiet", "--detach", source], check=True)
assert not subprocess.check_output(["git", "status", "--short"]) and Path(path).read_bytes() == fixture
new = check("author_revision")
assert old["exit"] == 3 and old["cases"].get("TestDetectSpuriousRecoveryWithRTO/timestamps") == "FAIL"
assert len(old["threshold_mismatch"]) == 1 and old["threshold_mismatch"][0][0] == "5"
assert new["exit"] == 0 and new["records"][0]["strategy"] == "linux-sandbox" and not new["records"][0]["cached"]
expected = set(cases) | {"TestDetectSpuriousRecoveryWithRTO/"+n for n in ("timestamps","no_timestamps","missing_timestamp","zero_echo")} | {"TestEifelFiniteApplicationFlight/"+n for n in ("original_ack","repair_ack")}
assert expected.issubset(new["cases"]) and all(status=="PASS" for status in new["cases"].values())
