"""Compare the Eifel response with in-order and reordered ACKs."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from xml.etree import ElementTree
from urllib.parse import unquote, urlsplit

root = Path(os.environ["RUNNER_TEMP"]) / "eifel-check"
owner = "//pkg/tcpip/transport/tcp/test/e2e:tcp_sack_test"
source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert source == "f23070977169b4084ae8f4a0b18fa139404467e9"
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
def check(name, owner, cases):
    bep = root / (name + ".bep")
    cmd = [root / "bazelisk", "--nosystem_rc", "--nohome_rc", "test", *flags,
           "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v", "--test_filter=^(" + "|".join(cases) + ")$",
           "--build_event_json_file=" + str(bep), owner]
    with (root / (name + ".console")).open("wb") as console:
        code = subprocess.run(cmd, stdout=console, stderr=subprocess.STDOUT).returncode
    records = []
    joined = ""
    xml_cases = {}
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
            xml_entry, = [entry for entry in result["testActionOutput"] if entry["name"] == "test.xml"]
            xml_uri = urlsplit(xml_entry["uri"])
            assert xml_uri.scheme == "file"
            for case in ElementTree.parse(unquote(xml_uri.path)).iter("testcase"):
                label = case.get("name")
                assert label and label not in xml_cases
                xml_cases[label] = ("FAIL" if case.find("failure") is not None or case.find("error") is not None
                                    else "SKIP" if case.find("skipped") is not None else "PASS")
            entry, = outputs
            uri = urlsplit(entry["uri"])
            assert uri.scheme == "file"
            data = Path(unquote(uri.path)).read_bytes()
            joined += data.decode(errors="replace")
            records.append({"status": result.get("status"), "cached": bool(result.get("cachedLocally") or result.get("executionInfo", {}).get("cachedRemotely")),
                            "strategy": result.get("executionInfo", {}).get("strategy"),
                            "log_sha256": hashlib.sha256(data).hexdigest()})
    statuses = {name: status for status, name in re.findall(r'^\s*--- (PASS|FAIL|SKIP): ([A-Za-z0-9_/]+) \(', joined, re.MULTILINE)}
    threshold = re.findall(r'ssthresh after [^\n=]+ = ([0-9]+), want ([0-9]+)', joined)
    detail = {"xml": xml_cases, "exit": code, "records": records, "cases": statuses, "threshold_mismatch": threshold}
    detail["test_diagnostics"] = [line.strip()[:400] for line in joined.splitlines()
                                  if re.search(r"tcp_(sack|rto_recovery)_test[.]go:[0-9]+:", line)][:16]
    detail["two_timeouts"] = re.findall(r"two RTOs before detection, original=(true|false): ssthresh=(\d+)", joined)
    detail["observed_thresholds"] = re.findall(r"threshold after cumulative ACK = (\d+)", joined)
    receipt[name] = detail
    (root / "receipts.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(name, detail, flush=True)
    return detail

other = "62277fb4a1eaaaf515d84d284ef080a71215f627"
common = "e3496657de25339f11754172e448b29f71882e7d"
subprocess.run(["git", "cherry-pick", "--no-commit", other], check=True)
tree = subprocess.check_output(["git", "write-tree"], text=True).strip()
assert tree == "d48e521b932b553f0469b3c70cacc0f5d569410f"
receipt.update(second=other, combined_tree=tree, common_base=common, verified=False)
directory = Path("pkg/tcpip/transport/tcp/test/e2e")
fixture = directory / "tcp_sack_reordered_response_test.go"
assert not fixture.exists()
fixture.write_bytes((root / fixture.name).read_bytes())
receipt["fixture"] = hashlib.sha256(fixture.read_bytes()).hexdigest()
build = directory / "BUILD"
contents = build.read_text()
before = 'srcs = ["tcp_sack_test.go"]'
assert contents.count(before) == 1
build.write_text(contents.replace(before, 'srcs = ["tcp_sack_test.go", "tcp_sack_reordered_response_test.go"]'))
case = "TestEifelResponseAfterReorderedACK"
genuine = "TestEifelGenuineLossAfterSpuriousDetection"
check("combined", owner, [case, genuine])
sender = Path("pkg/tcpip/transport/tcp/snd.go")
contents = sender.read_text()
before = "if s.inRecovery() && s.spuriousRecovery {"
assert contents.count(before) == 1
sender.write_text(contents.replace(before, "if (s.inRecovery() || s.spuriousTimeoutResumed) && s.spuriousRecovery {"))
receipt["corrected_sender"] = hashlib.sha256(sender.read_bytes()).hexdigest()
check("resumed", owner, [case, genuine])
for arm in ("combined", "resumed"):
    d = receipt[arm]
    record, = d["records"]
    assert record["strategy"] == "linux-sandbox" and not record["cached"]
    assert d["cases"] == d["xml"]
    assert d["cases"][case + "/in_order"] == "PASS"
    assert d["cases"][genuine] == "PASS"
    assert d["cases"][case + "/one_reordered_segment"] == ("FAIL" if arm == "combined" else "PASS")
    assert (d["exit"] != 0) == (arm == "combined")
    assert len(d["observed_thresholds"]) == 2
    for name, status in d["cases"].items():
        want = "FAIL" if arm == "combined" and name in (case, case + "/one_reordered_segment") else "PASS"
        assert status == want, (name, status, want)
receipt["verified"] = True
(root / "receipts.json").write_text(json.dumps(receipt, indent=2) + "\n")
