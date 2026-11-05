"""Check the two rename lock paths and the directory cursor's read lock."""
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

from qualification_report import xml_cases

task = Path(os.environ["RUNNER_TEMP"]) / "kernfs-children"
pkg = Path("pkg/sentry/fsimpl/kernfs")
analyzer = "//pkg/sentry/fsimpl/kernfs:kernfs_nogo"
unit = "//pkg/sentry/fsimpl/kernfs:kernfs_test"
changes = {
    "inode_impl_util.go": ("\tdst.mu.Lock()\n\tdefer dst.mu.Unlock()\n", ("return o.renameLocked(oldname, newname, child, dst)",)),
    "fd_impl_util.go": ("\tfd.children.mu.RLock()\n\tdefer fd.children.mu.RUnlock()\n", ("for it := fd.children.nthLocked(childIdx)",
                           "relOffset := fd.off - int64(len(fd.children.set))")),
}
version = subprocess.check_output(["bazel", "--version"], text=True).strip()
assert version == "bazel " + Path(".bazelversion").read_text().strip(), version
expected = set()
for phase in ("corrected", "missing"):
    if phase == "missing":
        for name, (guard, accesses) in changes.items():
            source = pkg / name
            original = source.read_text()
            assert original.count(guard) == 1, name
            edited = original.replace(guard, "")
            for access in accesses:
                lines = [i for i, line in enumerate(edited.splitlines(), 1) if access in line]
                assert len(lines) == 1
                expected.add((name, lines[0]))
            source.write_text(edited)
    owners = [analyzer, unit] if phase == "corrected" else [analyzer]
    bep = task / (phase + ".bep")
    run = subprocess.run(["bazel", "test", "--config=x86_64", "-c", "opt", "--jobs=4", "--keep_going",
                          "--build_tag_filters=", "--test_tag_filters=", "--strategy=TestRunner=sandboxed",
                          "--remote_cache=", "--remote_executor=", "--bes_backend=", "--nocache_test_results",
                          "--flaky_test_attempts=1", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_output=errors",
                          "--build_event_json_file=" + str(bep), *owners])
    result = {"bazel": version, "exit": run.returncode, "attempts": {}, "findings": {}}
    receipt = task / "receipts" / (phase + ".json")
    receipt.write_text(json.dumps(result))
    with bep.open() as events:
        for line in events:
            e = json.loads(line)
            if "testResult" in e:
                label = e["id"]["testResult"]["label"]
                attempt = e["testResult"]
                info = attempt.get("executionInfo", {})
                result["attempts"].setdefault(label, []).append([attempt["status"], info.get("strategy"),
                    bool(attempt.get("cachedLocally") or info.get("cachedRemotely"))])
    matched = {}
    for arch in ("amd64", "arm64"):
        path = Path("bazel-bin") / pkg / ("kernfs_nogo." + arch + ".findings")
        rows = (json.loads(path.read_bytes()) or []) if path.exists() else None
        if rows is not None:
            result["findings"][arch] = [{"file": Path(f["Position"]["Filename"]).name, "line": f["Position"]["Line"],
                                         "category": f["Category"], "message": f["Message"][:450]} for f in rows]
            matched[arch] = {(f["file"], f["line"]) for f in result["findings"][arch]
                             if f["category"] == "checklocks" and "mu" in f["message"]}
    if phase == "corrected":
        xml = Path("bazel-testlogs") / pkg / "kernfs_test/test.xml"
        if xml.is_file():
            (task / "receipts" / "kernfs-test.xml").write_bytes(xml.read_bytes())
            result["cases"] = xml_cases(xml)
            raw_log = xml.with_name("test.log").read_bytes()
            (task / "receipts" / "kernfs-test.log").write_bytes(raw_log)
            log = raw_log.decode()
            names = set(re.findall(r"^--- PASS: (Test\w+) \(", log, re.M))
            for case in ET.parse(xml).iter("testcase"):
                name = case.get("name", "")
                if (name.startswith("Test") and case.get("status") != "notrun" and case.get("result") != "suppressed"
                    and all(case.find(tag) is None for tag in ("skipped", "failure", "error"))):
                    names.add(name)
            result["passed_names"] = sorted(names)
            result["required_names"] = re.findall(r"^func (Test\w+)\(t \*testing\.T\)", (pkg / "kernfs_test.go").read_text(), re.M)
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    assert set(result["attempts"]) == set(owners)
    status = "PASSED" if phase == "corrected" else "FAILED"
    for attempts in result["attempts"].values():
        assert len(attempts) == 1
        outcome, strategy, cached = attempts[0]
        assert outcome == status and strategy in ("processwrapper-sandbox", "linux-sandbox") and not cached
    assert (run.returncode == 0 if phase == "corrected" else run.returncode == 3)
    assert all(matched.get(arch) == expected and len(result["findings"][arch]) == len(expected) for arch in ("amd64", "arm64"))
    if phase == "corrected":
        assert result["cases"]["failed"] == 0 and len(result["required_names"]) == 12
        assert set(result["required_names"]).issubset(result["passed_names"])
