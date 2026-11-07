"""Read the three uncached TestRunner outcomes from their original BEP and XML."""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

root = Path(os.environ["GITHUB_WORKSPACE"])
work = Path(os.environ["RUNNER_TEMP"]) / "syscall-test-sizes"
result = work / "receipt.json"
memory = "memory_accounting_test_runsc_systrap_directfs"
targets = {"processes_test_runsc_ptrace": ("large", "processes.cc"),
           memory + "_save": ("enormous", "memory_accounting.cc"),
           memory + "_save_resume": ("enormous", "memory_accounting.cc")}
prefix = "//test/syscalls:"
probe = ('while read -r key value unit; do if [[ "$key" == MemTotal: ]]; then ram="$value"; break; fi; '
         'done < /proc/meminfo; read -r threads < /proc/sys/kernel/threads-max; '
         'printf "SYSCALL_TEST_WORKER size=%s memory_kib=%s threads_max=%s\\n" '
         '"$TEST_SIZE" "$ram" "$threads"; exec "$@"')
patterns = {"dependency fetch": rb"Error downloading|Failed to fetch|Failed to download",
            "compilation": rb"compilation of rule|C\+\+ compilation of rule|fatal error:",
            "no toolchain": rb"No matching toolchain|no matching toolchains",
            "thread limit": rb"Resource temporarily unavailable|EAGAIN",
            "memory limit": rb"Cannot allocate memory|Out of memory|ENOMEM",
            "setup permission": rb"Operation not permitted|Permission denied",
            "sandbox bootstrap": rb"sandbox exited before the container started",
            "server terminated": rb"Server terminated abruptly",
            "no matching worker": rb"no registered executors|No registered executors|no routable executors"}
def classes(data):
    return [name for name, regex in patterns.items() if re.search(regex, data)]
def number(value):
    return isinstance(value, int) or isinstance(value, str) and value.isdecimal()
def write():
    result.write_text(json.dumps(receipt, indent=2) + "\n")
def digest(data):
    return hashlib.sha256(data).hexdigest()

source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert source == os.environ["PRODUCT"]
invocation = str(uuid.uuid4())
receipt = {"source": source, "invocation": invocation, "status": "INCOMPLETE", "targets": {}}
write()
bazel = [os.environ["BAZEL"], "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(work / "remote.bazelrc")]
common = ["--config=x86_64", "--jobs=4", "--remote_download_outputs=all",
          "--spawn_strategy=linux-sandbox", "--strategy=TestRunner=remote", "--noremote_local_fallback",
          "--modify_execution_info=^TestRunner$=+no-remote-cache", "--test_tag_filters=", "--build_tests_only", "--flaky_test_attempts=1", "--nocache_test_results",
          "--remote_timeout=900", "--test_timeout=900", "--keep_going"]
options = ["--run_under=" + shlex.join(["/bin/bash", "-c", probe, "syscall-worker"]),
           "--build_event_json_file=" + str(work / "events.json"), "--invocation_id=" + invocation]
start = time.monotonic()
with (work / "console").open("wb") as log:
    try:
        code = subprocess.run([*bazel, "test", *common, *options, *(prefix + name for name in targets)],
                              stdout=log, stderr=subprocess.STDOUT, timeout=95 * 60).returncode
    except subprocess.TimeoutExpired:
        code = 124
receipt.update(command_exit=code, seconds=round(time.monotonic() - start))
events = []
if (work / "events.json").exists():
    for line in (work / "events.json").open():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            receipt["incomplete_bep"] = True
receipt["started"] = [event["started"]["uuid"] for event in events if "started" in event]
console = (work / "console").read_bytes()
receipt["console"] = {"sha256": digest(console), "bytes": len(console), "classes": classes(console)}
for name, (size, fixture) in targets.items():
    row = receipt["targets"][name] = {"status": "INCOMPLETE", "root": prefix + name}
    attempts = [event["testResult"] for event in events if "testResult" in event and
                event["id"]["testResult"]["label"].lstrip("@") == prefix + name]
    owners = [event["testSummary"].get("overallStatus") for event in events if "testSummary" in event
              and event["id"]["testSummary"]["label"].lstrip("@") == prefix + name]
    row.update(owners=owners, attempts=[item.get("status") for item in attempts])
    directory = root / "bazel-testlogs/test/syscalls" / name
    for output in ("test.log", "test.xml"):
        path = directory / output
        if not path.exists():
            continue
        data = path.read_bytes()
        row[output] = {"sha256": digest(data), "bytes": len(data)}
        if output == "test.log":
            row["log_classes"] = classes(data)
            witnesses = re.findall(rb"^SYSCALL_TEST_WORKER size=(\w+) memory_kib=(\d+) threads_max=(\d+)$", data, re.M)
            row["workers"] = [{"size": s.decode(), "memory_kib": int(m), "threads_max": int(t)}
                              for s, m, t in witnesses if s in (b"large", b"enormous", b"medium", b"small")]
            continue
        expected = {".".join(pair) for pair in re.findall(r"^TEST\((\w+), (\w+)\)",
                    (root / "test/syscalls/linux" / fixture).read_text(), re.M)}
        cases = []
        try:
            tree = ET.fromstring(data)
        except ET.ParseError:
            row["invalid_xml"] = True
            continue
        for case in tree.iter("testcase"):
            identity = case.get("classname", "") + "." + case.get("name", "")
            status = ("failed" if case.find("failure") is not None or case.find("error") is not None
                      else "skipped" if case.find("skipped") is not None or case.get("status") == "notrun"
                      or case.get("result") in ("suppressed", "skipped") else "passed")
            cases.append({"name": identity if identity in expected else "unrecognized:" + digest(identity.encode()), "status": status})
        row["cases"] = cases
        row["all_cases"] = (collections.Counter(c["name"] for c in cases) == collections.Counter(sorted(expected))
                            and all(c["status"] == "passed" for c in cases))
    if len(attempts) == 1:
        detail = attempts[0]
        info = detail.get("executionInfo", {})
        strategy = info.get("strategy")
        row["remote"] = strategy == "remote" if strategy else None
        row["cached"] = bool(detail.get("cachedLocally") or info.get("cachedRemotely"))
        row["duration"] = detail.get("testAttemptDurationMillis") if number(detail.get("testAttemptDurationMillis")) else None
    if row["attempts"] == ["FAILED"] or owners == ["FAILED"]:
        row["status"] = "FAIL"
    elif (row["attempts"] == owners == ["PASSED"] and not row["cached"] and row["remote"] is True
          and row.get("all_cases") and len(row.get("workers", [])) == 1 and row["workers"][0]["size"] == size):
        row["status"] = "PASS"
    write()
if receipt["started"] == [invocation] and code == 0 and all(r["status"] == "PASS" for r in receipt["targets"].values()):
    receipt["status"] = "PASS"
elif any(r["status"] == "FAIL" for r in receipt["targets"].values()):
    receipt["status"] = "FAIL"
write()
print(json.dumps({k: v["status"] for k, v in receipt["targets"].items()}))
raise SystemExit(receipt["status"] != "PASS")
