"""Check the concrete call which drains detached NICs during Stack.Wait."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

root = Path(os.environ["RUNNER_TEMP"]) / "nic-exclude"
owner = "//pkg/tcpip/stack:stack_nogo"
source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert source == "39d28d4ef5d4a11aa5f8756d62bf99e1cd02816d"
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
    # Store only expected diagnostic lines and target-level architecture statuses.
    diagnostics = [line.strip() for line in joined.splitlines()
                   if "must not hold s.mu" in line and "to call closeRemovedNICs" in line]
    assert all(len(line) < 1800 and not re.search(
        r"(?i:authorization|x-buildbuddy-api-key)|gh[pousr]_|github_pat_", line)
               for line in diagnostics)
    result = {"exit": code, "records": records, "diagnostics": diagnostics,
              "passes": [arch for arch in ("amd64", "arm64") if arch + "...PASS" in joined],
              "fails": [arch for arch in ("amd64", "arm64") if arch + "...FAIL" in joined]}
    receipt[name] = result
    (root / "receipts.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(name, result, flush=True)
    return result

positive = check("released")
p = Path("pkg/tcpip/stack/stack.go")
text = p.read_text()
before = "\ts.mu.Unlock()\n\n\ts.closeRemovedNICs(removed, waitFor)"
after = "\ts.closeRemovedNICs(removed, waitFor)\n\ts.mu.Unlock()"
assert text.count(before) == 1
p.write_text(text.replace(before, after))
line = p.read_text().splitlines().index("\ts.closeRemovedNICs(removed, waitFor)") + 1
receipt["mutant_call_line"] = line
negative = check("held")
assert positive["exit"] == 0 and positive["passes"] == ["amd64", "arm64"]
assert negative["exit"] and len(negative["diagnostics"]) >= 2
assert all("stack.go:" + str(line) + ":" in row for row in negative["diagnostics"])
