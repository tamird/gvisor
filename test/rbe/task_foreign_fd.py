"""Apply the existing PIDFD qualifier to the foreign FD lookup product."""

import hashlib
import json
from pathlib import Path
import traceback

import pidfd_locks as shared


shared.CASES = ["InvalidPidfd", "GetfdUnknown", "GetfdInvalidFlags", "GetfdWorks",
                "GetfdFailsWhenUnprivileged"]
shared.SYSCALLS = ["//test/syscalls:pidfd_test_native",
                   "//test/syscalls:pidfd_test_runsc_systrap_directfs"]
analyzers = ["//pkg/sentry/kernel:kernel_nogo", "//pkg/sentry/kernel:kernel_test_nogo",
             "//pkg/sentry/fsimpl/proc:proc_nogo", "//pkg/sentry/fsimpl/proc:proc_test_nogo"]
assert shared.git("rev-parse", "HEAD").decode().strip() == shared.FIXED
assert Path(".bazelversion").read_text().strip() == "9.3.1"
source = Path("test/syscalls/linux/pidfd_test.cc").read_text()
assert all(source.count("TEST(PidfdTest, " + case + ")") == 1 for case in shared.CASES)
result = {"status": "INCOMPLETE", "source": shared.FIXED, "phases": {},
          "scope": "five existing PIDFD cases on native/systrap; four dual-architecture analyzers",
          "package_runtime_tests": "not selected", "race_regression": "not selected",
          "bazel_version": "9.3.1", "executor_architecture": "amd64",
          "analyzer_target_architectures": ["amd64", "arm64"],
          "donor": "2a9e72475b37fbcb46e24d44a785b0e5daa661d7",
          "donor_sha256": hashlib.sha256(Path(shared.__file__).read_bytes()).hexdigest(),
          "profile_files": {name: hashlib.sha256((shared.ROOT / "gnu-platform" / name).read_bytes()).hexdigest()
                            for name in ("BUILD.bazel", "MODULE.bazel")}}
shared.write(result)
for name, labels, options in [
    ("analyzers", analyzers, []),
    ("syscalls", shared.SYSCALLS,
     ["--test_env=GTEST_FILTER=" + ":".join("PidfdTest." + case for case in shared.CASES)]),
]:
    try:
        shared.phase(result, name, shared.FIXED, labels, options)
    except Exception as error:
        item = result["phases"][name]
        item["collection_error"] = {"type": type(error).__name__,
                                    "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
    item = result["phases"][name]
    item["test_finish_millis"] = []
    try:
        events = (shared.PRIVATE / name / "raw.json").read_text().splitlines()
        for line in events:
            event = json.loads(line)
            if event.get("id", {}).get("testResult", {}).get("label") in labels:
                attempt = event["testResult"]
                item["test_finish_millis"].append(int(attempt["testAttemptStartMillis"]) +
                                                  int(attempt["testAttemptDurationMillis"]))
    except (OSError, ValueError, KeyError, TypeError):
        item["test_timing_incomplete"] = True
    item["test_timing_complete"] = (not item.get("test_timing_incomplete") and
                                    len(item["test_finish_millis"]) == len(labels))
    result["phases"][name]["verified_at"] = shared.now()
    shared.write(result)
result["status"] = "PASS" if all(p["verified"] for p in result["phases"].values()) else "INCOMPLETE"
if any(p["status"] == "FAIL" for p in result["phases"].values()):
    result["status"] = "FAIL"
result["verified_at"] = shared.now()
shared.write(result)
raise SystemExit(result["status"] != "PASS")
