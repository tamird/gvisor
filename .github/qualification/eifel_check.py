"""Verify recovery after a spurious timeout with both TCP proposals."""
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
resume = ("TestSpuriousRTOResumesWithUnsentData", "TestSpuriousRTOFastRetransmit",
          "TestSpuriousRTOPartialFirstACK", "TestSpuriousRTORACKRetransmit")
drain = "TestRTORecoveryDrainsLostWindow"

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
    receipt[name] = detail
    (root / "receipts.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(name, detail, flush=True)
    return detail

sack = owner
rack = owner.replace(":tcp_sack_test", ":tcp_rack_test")
recovery = "1b9768b199524246fe8e4fa66a0b623069778780"
base = "f51a205a8f4afbc5ab41aded070df74951b081ea"
receipt.update(recovery=recovery, common_comparison_base=base, verified=False)

def select_tree(parent, expected):
    subprocess.run(["git", "switch", "--quiet", "--detach", parent], check=True)
    subprocess.run(["git", "cherry-pick", "--no-commit", source], check=True)
    assert subprocess.check_output(["git", "write-tree"], text=True).strip() == expected
    receipt[parent] = expected

def discard_comparison():
    subprocess.run(["git", "restore", "--staged", "--worktree", "--source=HEAD", "--",
                    "pkg/tcpip/transport/tcp/snd.go",
                    "pkg/tcpip/transport/tcp/test/e2e/tcp_sack_test.go"], check=True)
    assert not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])

check("published", sack, resume)
select_tree(base, "55f7129413f601ea9e688b1839a5dad0e133416a")
check("on_common_base", sack, resume)
discard_comparison()
select_tree(recovery, "aea8e6d1e33a7245fea22651f42e7eec19ac9afb")
check("combined", sack, resume)
check("combined_genuine_loss", rack, [drain])

sender = Path("pkg/tcpip/transport/tcp/snd.go")
rack_source = Path("pkg/tcpip/transport/tcp/rack.go")
def replace_once(p, before, after):
    contents = p.read_text()
    assert contents.count(before) == 1, before
    p.write_text(contents.replace(before, after))

anchor = "// detectLoss "
replace_once(sender, anchor, """// A detected spurious timeout has stopped retransmitting the old flight.
func (s *sender) suppressFastRecovery() bool {
	return s.state == tcpip.RTORecovery && !s.spuriousTimeoutResumed
}

""" + anchor)
replace_once(sender, "if s.state == tcpip.RTORecovery {\n\t\treturn false\n\t}\n\n\t// If RACK is enabled",
             "if s.suppressFastRecovery() {\n\t\treturn false\n\t}\n\n\t// If RACK is enabled")
replace_once(sender, "numLost > 0 && !s.FastRecovery.Active && s.state != tcpip.RTORecovery {",
             "numLost > 0 && !s.FastRecovery.Active && !s.suppressFastRecovery() {")
replace_once(rack_source, "numLost == 0 || rc.snd.state == tcpip.RTORecovery {",
             "numLost == 0 || rc.snd.suppressFastRecovery() {")
receipt["guard_blobs"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (sender, rack_source)}
check("resumed", sack, resume)
check("resumed_genuine_loss", rack, [drain])
genuine_cases = {drain, *(drain + "/" + mode for mode in ("rack", "sack", "rack_reordered", "rack_timer"))}
for arm in ("published", "on_common_base", "combined", "resumed", "combined_genuine_loss", "resumed_genuine_loss"):
    detail = receipt[arm]
    record, = detail["records"]
    assert record["strategy"] == "linux-sandbox" and not record["cached"], arm
    expected = dict.fromkeys(genuine_cases if "genuine_loss" in arm else resume, "PASS")
    if arm == "combined":
        for case in resume[1:]:
            expected[case] = "FAIL"
    assert detail["xml"] == expected and detail["cases"] == expected, arm
    assert (detail["exit"] != 0) == (arm == "combined"), arm
receipt["verified"] = True
(root / "receipts.json").write_text(json.dumps(receipt, indent=2) + "\n")
