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
assert source == "f85916767e5f2a21315076bc8661be567dc736f7"
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
    finished = []
    joined = ""
    xml_cases = {}
    if bep.is_file():
        for line in bep.open():
            event = json.loads(line)
            if "finished" in event:
                finished.append(event["finished"].get("exitCode",{}).get("code",0))
            if event["id"].get("testResult", {}).get("label") != owner:
                continue
            result = event.get("testResult", {})
            outputs = [f for f in result.get("testActionOutput", []) if f["name"] == "test.log"]
            if not outputs:
                records.append({"aborted": event.get("aborted", {}).get("reason"), "status": result.get("status")})
                continue
            xml_entry, = [entry for entry in result["testActionOutput"] if entry["name"] == "test.xml"]
            xml_uri = urlsplit(xml_entry["uri"])
            assert xml_uri.scheme == "file" and not xml_uri.netloc
            xml_sha = hashlib.sha256(Path(unquote(xml_uri.path)).read_bytes()).hexdigest()
            assert xml_entry.get("digest", xml_sha) == xml_sha
            for case in ElementTree.parse(unquote(xml_uri.path)).iter("testcase"):
                label = case.get("name")
                assert label and label not in xml_cases
                xml_cases[label] = ("FAIL" if case.find("failure") is not None or case.find("error") is not None
                                    else "SKIP" if case.find("skipped") is not None or case.get("status") in ("notrun", "skipped") or case.get("result") in ("suppressed", "skipped") else "PASS")
            entry, = outputs
            uri = urlsplit(entry["uri"])
            assert uri.scheme == "file"
            assert not uri.netloc
            data = Path(unquote(uri.path)).read_bytes()
            assert entry.get("digest",hashlib.sha256(data).hexdigest()) == hashlib.sha256(data).hexdigest()
            joined += data.decode(errors="replace")
            records.append({"status": result.get("status"), "cached": bool(result.get("cachedLocally") or result.get("executionInfo", {}).get("cachedRemotely")),
                            "strategy": result.get("executionInfo", {}).get("strategy"),
                            "log_sha256": hashlib.sha256(data).hexdigest(), "xml_sha256": xml_sha})
    starts = re.findall(r'^\s*=== RUN\s+([^\s()]+)\s*$', joined, re.MULTILINE)
    ends = re.findall(r'^\s*--- (PASS|FAIL|SKIP): ([^\s()]+) \(', joined, re.MULTILINE)
    statuses = {name:status for status,name in ends}
    assert len(starts) == len(set(starts)) and len(ends) == len(statuses)
    assert set(starts) == set(statuses)
    threshold = re.findall(r'ssthresh after [^\n=]+ = ([0-9]+), want ([0-9]+)', joined)
    detail = {"finished": finished, "xml": xml_cases, "exit": code, "records": records, "cases": statuses, "threshold_mismatch": threshold}
    detail["test_diagnostics"] = [line.strip()[:400] for line in joined.splitlines()
                                  if re.search(r"tcp_(sack|rto_recovery)_test[.]go:[0-9]+:", line)][:16]
    detail["two_timeouts"] = re.findall(r"two RTOs before detection, original=(true|false): ssthresh=(\d+)", joined)
    receipt[name] = detail
    (root / "receipts.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(name, code, len(xml_cases), flush=True)
    return detail


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()

parent = "1ebae73b9a566f63fc85e83f4b554a0a409a13ec"
recovery = "1b9768b199524246fe8e4fa66a0b623069778780"
sender_path = "pkg/tcpip/transport/tcp/snd.go"
test_path = "pkg/tcpip/transport/tcp/test/e2e/tcp_sack_test.go"
assert git("rev-parse", "HEAD^") == parent
assert git("rev-parse", parent+"^{tree}") == "d48e521b932b553f0469b3c70cacc0f5d569410f"
assert set(git("diff", "--name-only", parent, source).splitlines()) == {sender_path,test_path}
receipt.update(author_parent=parent, recovery=recovery, author_test_blob=git("rev-parse",source+":"+test_path))
sack = owner
rack = owner.replace(":tcp_sack_test", ":tcp_rack_test")
reorder = "TestSpuriousRTOReorderAfterResume"
genuine = "TestEifelGenuineLossAfterSpuriousDetection"
cases = (*resume,reorder,"TestSpuriousRTOLossAfterReorderedACK",genuine)
expected = set(cases) | {reorder+"/rack="+a+"/reorder="+b for a in ("true","false") for b in ("true","false")} | {genuine+"/timeout",genuine+"/rack"}
drain_cases = {drain, *(drain + "/" + v for v in ("rack", "sack", "rack_reordered", "rack_timer"))}
git("restore","--source="+parent,"--",sender_path)
assert git("diff","--name-only")==sender_path and git("hash-object",sender_path)==git("rev-parse",parent+":"+sender_path)
check("pair_before", sack, cases)
git("restore","--source=HEAD","--",sender_path)
assert not git("diff","--name-only")
check("author_fix", sack, cases)
git("cherry-pick","--no-commit",recovery)
assert git("write-tree") == "9dd588eb22f9bdb5be21e5fcfb59055cd0997c92"
assert git("hash-object",test_path)==receipt["author_test_blob"]
receipt["combined_tree"]=git("write-tree")
check("all_three",sack,cases)
check("all_three_drain",rack,[drain])
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

git("add","--",str(sender),str(rack_source))
receipt["composition_tree"]=git("write-tree")
check("composition_fix",sack,cases)
check("composition_fix_drain",rack,[drain])
for name in ("pair_before","author_fix","all_three","all_three_drain","composition_fix","composition_fix_drain"):
    d=receipt[name]
    record,=d["records"]
    assert record["strategy"]=="linux-sandbox" and not record["cached"], name
    assert (keys:=set(d["cases"])) == (drain_cases if name.endswith("_drain") else expected), (name,keys)
    assert d["cases"] == d["xml"] and set(d["cases"].values()) <= {"PASS","FAIL"}
    failed = "FAIL" in d["cases"].values()
    assert d["finished"] == [d["exit"]] and d["exit"] == (3 if failed else 0)
    assert record["status"] == ("FAILED" if failed else "PASSED")
receipt["measurements_complete"] = True
(root/"receipts.json").write_text(json.dumps(receipt,indent=2)+"\n")
