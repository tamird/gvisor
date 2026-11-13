"""Read the actual log files from both target configurations by their BEP digests."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

WORK = Path(os.environ["RUNNER_TEMP"]) / "paired-stub-guest"
TARGETS = ("//sandboxexec/sandbox/python:sandbox_py_test", "//vdso:vdso_test")
SOURCE = "48f457b4e040312422b8e28b97081998d1d81297"
PRIVATE = re.compile(r"authorization|bearer |api[-_]key|remote_header|bes_header|github_pat_|ghp_|BEGIN .*PRIVATE KEY", re.I)


def read_tests(arch: str, cpu: str) -> bool:
    bep = WORK / f"python-{arch}.bep"
    platform = "//tools/bazeldefs:linux_" + arch
    cmd = [str(WORK / "bazel9"), "--host_jvm_args=-Xmx10g", "--nosystem_rc", "--nohome_rc",
           "--bazelrc=" + str(WORK / "diagnostics.rc"), "test", "--config=" + cpu,
           "--host_platform=" + platform, "--extra_execution_platforms=" + platform,
           "--remote_default_exec_properties=Arch=" + arch, "--distdir=" + str(WORK / "dist"),
           "--keep_going", "--jobs=64", "--strip=never", "--test_env=CGROUPV2=true",
           "--test_env=GO_TEST_WRAP_TESTV=1", "--nocache_test_results", "--flaky_test_attempts=1",
           "--strategy=TestRunner=remote", "--modify_execution_info=^TestRunner$=+no-remote-cache",
           "--remote_download_regex=.*test[.]log$", "--test_output=errors",
           "--build_event_json_file=" + str(bep)]
    report = {"arch": arch, "source": SOURCE, "tests": {}}
    # Separate consoles allow the output to be attributed to a single test even
    # when a Build Event Protocol file record lacks an original CAS digest.
    for label in TARGETS:
        with (WORK / f"python-{arch}.log").open("w+") as console:
            result = subprocess.run([*cmd, label], stdout=console, stderr=subprocess.STDOUT)
            console.seek(0)
            output = console.read()
        row = {"exit": result.returncode}
        report["tests"][label] = row
        for line in bep.open():
            event = json.loads(line)
            if "started" in event:
                row["invocation"] = event["started"]["uuid"]
            if "testResult" not in event:
                continue
            assert event["id"]["testResult"]["label"] == label
            test = event["testResult"]
            row["status"] = test.get("status")
            outfile, = [f for f in test["testActionOutput"] if f["name"] == "test.log"]
            digest = outfile.get("digest") or outfile["uri"].split("/")[-2]
            row["bep_digest"] = digest
            rel = label.removeprefix("//").replace(":", "/") + "/test.log"
            for path in Path("bazel-out").glob("*/testlogs/" + rel):
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() == digest:
                    row["log_sha256"] = digest
                    output = data.decode(errors="replace")
                    break
        lines = [s for s in output.splitlines() if not PRIVATE.search(s)]
        error = re.compile(r"error|failed|no such|read.only|not permitted|traceback|readelf|cannot|permission denied", re.I)
        hits = [i for i,s in enumerate(lines) if error.search(s) and not s.startswith("[")]
        rows = set()
        for i in hits[:90]:
            rows.update(range(max(i-1, 0), min(i+3, len(lines))))
        row["excerpt"] = [lines[i][:1200] for i in sorted(rows)]
    (WORK / f"python-{arch}-tests.json").write_text(json.dumps(report, indent=2, sort_keys=True)+"\n")
    return all(r.get("status") in ("PASSED","FAILED") and (r.get("log_sha256") or r.get("excerpt"))
               for r in report["tests"].values())


if __name__ == "__main__":
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    results = [read_tests("amd64","x86_64"), read_tests("arm64","aarch64")]
    sys.exit(0 if all(results) else 1)
