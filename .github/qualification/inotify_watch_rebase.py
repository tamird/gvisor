"""Read original Bazel test results for the rebased inotify watch API."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET

job = Path(os.environ["CHECK_DIR"])
bazel = ["bazel", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g"]
options = [
    "--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--local_resources=memory=8192",
    "--remote_cache=", "--remote_executor=", "--bes_backend=", "--keep_going",
    "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
    "--strategy=TestRunner=local", "--nocache_test_results", "--runs_per_test=1",
    "--flaky_test_attempts=1", "--test_sharding_strategy=disabled", "--test_output=errors",
    "--test_timeout=600", "--build_event_json_file_path_conversion=false",
]
source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert source == "aab0e06b4cf72fe170423d2b70b9cbc3eb9d86a6"
assert platform.machine() == "x86_64"
assert subprocess.check_output(["bazel", "--version"], text=True).strip() == "bazel 9.3.1"
syscall = "//test/syscalls:inotify_test_native"
kernfs = "//pkg/sentry/fsimpl/kernfs:kernfs_test"
analyzers = ["//pkg/sentry/vfs:vfs_nogo", "//pkg/sentry/syscalls/linux:linux_nogo"]
mask_cases = [
    "MaskCreatePreservesExistingWatch", "HardlinksReuseSameWatch", "AddWatchWithInvalidEventMaskFails",
    "DuplicateWatchReturnsSameWatchDescriptor", "MaskAddMergesWithExistingEventMask", "OneShot",
]
eviction_cases = [
    "TestRmdirInotifyDeleteSelfBeforeParentDelete", "TestRmdirInotifyWithOpenFDDefersDeleteSelf",
    "TestEvictCachedDentriesPreservesWatched",
]


def run(name, targets, args, expected):
    invocation = str(uuid.uuid4())
    bep = job / (name + ".bep")
    cmd = bazel + ["test", *options, "--invocation_id=" + invocation,
                   "--build_event_json_file=" + str(bep), *args, *targets]
    status = subprocess.run(cmd).returncode
    record = dict(source=source, os=platform.release(), invocation=invocation, exit=status, targets={})
    try:
        events = [json.loads(line) for line in bep.open()]
        assert [e["started"]["uuid"] for e in events if "started" in e] == [invocation]
        for target in targets:
            row = record["targets"][target] = {}
            result, = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label") == target]
            row["status"] = result["status"]
            row["local"] = result.get("executionInfo", {}).get("strategy") == "local"
            row["cached"] = bool(result.get("cachedLocally") or result.get("executionInfo", {}).get("cachedRemotely"))
            logdir = Path("bazel-testlogs", target[2:].replace(":", "/"))
            data = (logdir / "test.xml").read_bytes()
            row["xml_sha256"] = hashlib.sha256(data).hexdigest()
            log = (logdir / "test.log").read_bytes()
            row["log_sha256"] = hashlib.sha256(log).hexdigest()
            if target == syscall:
                cases = list(ET.fromstring(data).iter("testcase"))
                assert all(c.get("classname") == "Inotify" and c.get("status") != "notrun"
                           and c.get("result") not in ("skipped", "suppressed")
                           and all(c.find(k) is None for k in ("error", "failure", "skipped")) for c in cases)
                row["passed"] = sorted(c.get("name") for c in cases)
            elif target == kernfs:
                row["passed"] = sorted(re.findall(r"^--- PASS: (Test\w+) ", log.decode(), re.M))
            if expected:
                assert row["passed"] == sorted(expected), row
            assert row["status"] == "PASSED" and row["local"] and not row["cached"], row
        assert status == 0
        record["verdict"] = "PASS"
    finally:
        (job / "results" / (name + ".json")).write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record), flush=True)


groups = [
    ("linux", [syscall], ["--test_env=GTEST_FILTER=" + ":".join("Inotify." + c for c in mask_cases)], mask_cases),
    ("kernfs", [kernfs], ["--test_filter=^(" + "|".join(eviction_cases) + ")$", "--test_arg=-test.v"], eviction_cases),
    ("nogo", analyzers, [], []),
]
failures = []
for group in groups:
    try:
        run(*group)
    except Exception as exc:
        print(group[0], type(exc).__name__, exc, flush=True)
        failures.append(group[0])
if failures:
    raise SystemExit("Failed groups: " + ", ".join(failures))
