"""Exercise the NIC info contract in the stack package's real Nogo target."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import unquote, urlsplit

root = Path(os.environ["RUNNER_TEMP"]) / "nic-info-lock"
artifacts = root / "results"
artifacts.mkdir()
target = "//pkg/tcpip/stack:stack_nogo"
arches = ("amd64", "arm64")
stack = Path("pkg/tcpip/stack/stack.go")
nic = Path("pkg/tcpip/stack/nic.go")
candidate = stack.read_text()
fields = nic.read_text()

def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()

revision = git("rev-parse", "HEAD")
base = git("rev-parse", "HEAD^")
assert revision == os.environ["SOURCE"]
assert base == "f5828756d24ee60ebce2cb407dbb06dd853ad7ca"
original = subprocess.check_output(["git", "show", base + ":" + str(stack)], text=True)
assert git("diff-tree", "--no-commit-id", "--name-only", "-r", revision) == str(stack)
before = "// precondition: s.mu is held.\nfunc (s *Stack) nicInfo("
after = "// +checklocksread:s.mu\nfunc (s *Stack) nicInfo("
assert original.count(before) == 1 and original.replace(before, after) == candidate
report = {"source": revision, "base": base, "target": target, "arms": {}}

def save():
    (artifacts / "results.json").write_text(json.dumps(report, indent=2) + "\n")

def analyze(arm):
    bep = root / (arm + ".bep")
    args = [str(root / "bazelisk"), "--nosystem_rc", "--nohome_rc", "test",
            "--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--local_resources=memory=8192",
            "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
            "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
            "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
            "--strategy=TestRunner=linux-sandbox", "--sandbox_default_allow_network=false",
            "--nocache_test_results", "--test_sharding_strategy=disabled", "--flaky_test_attempts=1",
            "--test_timeout=180", "--test_output=errors", "--color=no", "--curses=no", "--noshow_progress",
            "--build_event_json_file=" + str(bep), target]
    with (artifacts / (arm + ".console")).open("wb") as console:
        status = subprocess.run(args, stdout=console, stderr=subprocess.STDOUT).returncode
    info = {"exit": status, "stackGo": git("hash-object", str(stack)), "nicGo": git("hash-object", str(nic))}
    report["arms"][arm] = info
    save()
    results = []
    for line in bep.read_text().splitlines():
        event = json.loads(line)
        if event["id"].get("testResult", {}).get("label") == target:
            results.append(event["testResult"])
    result, = results
    assert not result.get("cachedLocally") and result["executionInfo"]["strategy"] == "linux-sandbox"
    output, = (f for f in result["testActionOutput"] if f["name"] == "test.log")
    uri = urlsplit(output["uri"])
    assert uri.scheme == "file"
    raw = Path(unquote(uri.path)).read_bytes()
    (artifacts / (arm + ".test.log")).write_bytes(raw)
    log = raw.decode()
    info.update(status=result["status"], logSHA256=hashlib.sha256(raw).hexdigest(),
                passed=[arch for arch in arches if arch + "...PASS" in log], strategy=result["executionInfo"]["strategy"],
                diagnostics=[line.strip() for line in log.splitlines() if "checklocks:" in line])
    save()
    print(arm, result["status"], info["passed"], len(info["diagnostics"]), flush=True)
    return info

controls = """
// These helpers are compiled only by the isolated static-analysis controls.
func sharedNICInfoControl(s *Stack, n *nic) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	s.nicInfo(n, n.id)
}

func exclusiveNICInfoControl(s *Stack, n *nic) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.nicInfo(n, n.id)
}

func unlockedNICInfoControl(s *Stack, n *nic) {
	s.nicInfo(n, n.id)
}
"""
try:
    positive = analyze("candidate")
    stack.write_text(original + controls)
    baseline = analyze("stock-with-controls")
    stack.write_text(candidate + controls)
    unlocked_line = len(stack.read_text().splitlines()) - 1
    assert stack.read_text().splitlines()[unlocked_line - 1].strip() == "s.nicInfo(n, n.id)"
    report["unlockedLine"] = unlocked_line
    negative = analyze("candidate-with-controls")
    stack.write_text(candidate)
    name_field = "\tname    string\n"
    primary_field = "// Primary is the main controlling interface in a bonded setup.\n\tPrimary *nic"
    assert fields.count(name_field) == fields.count(primary_field) == 1
    nic.write_text(fields.replace(name_field, "\t// +checklocks:stack.mu\n"+name_field)
                   .replace(primary_field, primary_field.replace("\n", "\n\t//\n\t// +checklocks:stack.mu\n")))
    analyze("field-guards")
finally:
    stack.write_text(candidate)
    nic.write_text(fields)
    save()
for passing in (positive, baseline):
    assert passing["exit"] == 0 and passing["status"] == "PASSED" and passing["passed"] == list(arches), passing
diagnostics = negative["diagnostics"]
assert negative["exit"] == 3 and negative["status"] == "FAILED"
assert len(diagnostics) == len(arches), diagnostics
for arch in arches:
    matching = [line for line in diagnostics if "ARCH=" + arch in line]
    assert len(matching) == 1 and "must hold s.mu" in matching[0] and "to call nicInfo" in matching[0], matching
    assert "stack.go:" + str(unlocked_line) + ":" in matching[0], matching
