"""Verify the sender invokes CUBIC idle restart on a drained connection."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from xml.etree import ElementTree
from urllib.parse import unquote, urlsplit

root = Path(os.environ["RUNNER_TEMP"]) / "idle-restart"
owner = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert source == "351a86281ce1b4089be794ac02091bf6c725ff19"
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
    detail = {"finished": finished, "xml": xml_cases, "exit": code, "records": records, "cases": statuses}
    detail["samples"] = re.findall(r"idle=(\S+) before=(\d+) ssthresh=(\d+) rto=(\S+) restart=(\d+) after=(\d+)", joined)
    detail["test_diagnostics"] = [line.strip()[:360] for line in joined.splitlines()
                                  if re.search(r"(tcp_test|context)[.]go:\d+:|panic:|timed out", line)][:24]
    if not records:
        detail["build_error"] = [line.strip()[:360] for line in (root/(name+".console")).read_text(errors="replace").splitlines()
                                 if "error" in line.lower() or "failed" in line.lower()][-8:]
    receipt[name] = detail
    (root / "receipts.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(name, code, len(xml_cases), flush=True)
    return detail


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


test_path = "pkg/tcpip/transport/tcp/test/e2e/tcp_test.go"
sender = Path("pkg/tcpip/transport/tcp/snd.go")
assert git("rev-parse", "HEAD^") == "9ee2906f518e1d0f9fd109e9e1feb2812096418b"
assert git("diff", "--name-only", "HEAD^", "HEAD") == test_path
receipt["test_blob"] = git("rev-parse", "HEAD:" + test_path)
base = sender.read_text()
call = "\t\t\ts.cc.HandleWindowRestart()\n"
assert base.count(call) == 1
assert git("status", "--porcelain") == ""
name = "TestCubicIdleRestart"
case_names = {name, name + "/short", name + "/long"}

for arm in ("stock", "without_callback"):
    if arm == "without_callback":
        sender.write_text(base.replace(call, ""))
        assert git("diff", "--name-only") == str(sender)
    check(arm, owner, [name])

for arm in ("stock", "without_callback"):
    d = receipt[arm]
    assert set(d["cases"]) == case_names and d["cases"] == d["xml"], d
    record, = d["records"]
    assert record["strategy"] == "linux-sandbox" and not record["cached"], d
    assert d["finished"] == [d["exit"]]
    assert d["cases"][name + "/short"] == "PASS"
    assert len(d["samples"]) == 2, d
    assert d["cases"][name + "/long"] == ("PASS" if arm == "stock" else "FAIL")
    assert d["exit"] == (0 if arm == "stock" else 3)
    assert record["status"] == ("PASSED" if arm == "stock" else "FAILED")
stock, mutant = (dict((v[0], v[1:]) for v in receipt[arm]["samples"]) for arm in ("stock", "without_callback"))
assert set(stock) == set(mutant) == {"1ms", "30s"}
assert stock["1ms"] == mutant["1ms"]
assert stock["30s"][:-1] == mutant["30s"][:-1]
assert stock["30s"][-2:] == ("10", "10") and int(mutant["30s"][-1]) > 10
receipt["verified"] = True
(root/"receipts.json").write_text(json.dumps(receipt, indent=2) + "\n")
