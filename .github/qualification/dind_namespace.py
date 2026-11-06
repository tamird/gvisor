import json
import os
from pathlib import Path
import re
import subprocess
import zipfile
import xml.etree.ElementTree as ET

root = Path(os.environ["RUNNER_TEMP"]) / "dind-stream"
draft = "fdd009fd9c0752b3f9fc3bf1a8c3deeff105a8c3"
fix = "428e3d67c87363bc16be64c80e8fe58a6a46c467"
test_file = "test/syscalls/linux/network_namespace.cc"
name = "NetworkNamespaceTest.Setns"
panic = b"registerChild called on a non-root namespace"
results = {}

def record():
    (root / "namespace.json").write_text(json.dumps(results, indent=2) + "\n")

def git(*args):
    subprocess.run(["git", *args], check=True)

def observe(source, label, platform):
    target = "network_namespace_test_" + platform
    rec = results[label] = {"source": source, "target": target}
    record()
    bep = root / (label + ".bep")
    log = root / (label + ".log")
    with log.open("wb") as stream:
        status = subprocess.run([os.environ["BAZEL"], "test", "--config=x86_64", "-c", "opt",
            "--keep_going", "--jobs=3", "--test_sharding_strategy=disabled", "--test_timeout=300",
            "--strategy=TestRunner=local", "--nocache_test_results", "--flaky_test_attempts=1",
            "--run_under=/usr/bin/sudo -n -E --", "--noshow_progress", "--color=no", "--curses=no",
            "--build_event_json_file=" + str(bep), "--test_env=GTEST_FILTER=" + name,
            "//test/syscalls:" + target], stdout=stream, stderr=subprocess.STDOUT).returncode
    rec["exit"] = status
    rec["test_results"] = []
    if bep.exists():
        for line in bep.open():
            event = json.loads(line)
            if event.get("id", {}).get("testResult", {}).get("label") == "//test/syscalls:" + target:
                t = event["testResult"]
                rec["test_results"].append([t.get("status"), t.get("cachedLocally", False)])
    rec["diagnostics"] = [l[-650:] for l in log.read_text(errors="replace").splitlines()
                          if re.search(r"ERROR:|command not found|No such file|not allowed|ermission denied", l)][:8]
    if rec["test_results"]:
        directory = Path("bazel-testlogs/test/syscalls") / target
        data = (directory / "test.log").read_bytes()
        xml = directory / "test.xml"
        if xml.exists():
            rec["xml"] = [{"name": c.get("name"), "result": c.get("result"),
                "status": c.get("status"), "children": [v.tag for v in c]}
                for c in ET.parse(xml).iter("testcase")][:15]
        rec["test_diagnostics"] = [l.strip()[:400] for l in data.decode(errors="replace").splitlines()
            if re.search(r"(?i)runtime error|error starting|permission denied|not permitted|unable to|failed to|panic:", l)][:10]
        rec["gtest"] = [l.strip()[:200] for l in data.decode(errors="replace").splitlines()
                        if name in l and re.search(r"\[\s*(?:OK|RUN|SKIPPED|FAILED)\s*\]", l)][:20]
        rec["panic"] = panic in data
        archive = directory / "test.outputs/outputs.zip"
        if archive.exists() and not rec["panic"]:
            with zipfile.ZipFile(archive) as z:
                for entry in z.infolist():
                    if entry.file_size <= 32 * 1024 * 1024 and panic in z.read(entry):
                        rec["panic"] = True
                        break
    record()
    print(label, rec, flush=True)

git("switch", "--detach", draft)
git("restore", "--source=" + fix, "--", test_file)
try:
    observe(draft, "linux", "native")
    observe(draft, "draft", "runsc_systrap_directfs")
finally:
    git("restore", "--", test_file)
git("switch", "--detach", fix)
observe(fix, "corrected", "runsc_systrap_directfs")
for label in ("linux", "corrected"):
    r = results[label]
    assert r["exit"] == 0 and r["test_results"] == [["PASSED", False]]
    assert any(re.search(r"\[\s*OK\s*\]\s+" + re.escape(name), line) for line in r.get("gtest", []))
r = results["draft"]
assert r["exit"] == 3 and r["test_results"] and r["test_results"][0][0] == "FAILED" and r.get("panic")
