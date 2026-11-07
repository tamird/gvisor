"""Read the three uncached TestRunner outcomes from their original BEP and XML."""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse

root = Path(os.environ["GITHUB_WORKSPACE"])
work = Path(os.environ["RUNNER_TEMP"]) / "syscall-test-sizes"
result = work / "receipt.json"
memory = "memory_accounting_test_runsc_systrap_directfs"
targets = {"processes_test_runsc_ptrace": ("large", "processes.cc"),
           memory + "_save": ("enormous", "memory_accounting.cc"),
           memory + "_save_resume": ("enormous", "memory_accounting.cc")}
prefix = "//test/syscalls:"
probe = ('set -eu; arch=$(/bin/uname -m); test "$arch" = x86_64; '
         '/bin/grep -Eq "^vendor_id[[:space:]]*:" /proc/cpuinfo; '
         'while read -r key value unit; do if [[ "$key" == MemTotal: ]]; then ram="$value"; break; fi; '
         'done < /proc/meminfo; read -r threads < /proc/sys/kernel/threads-max; '
         'printf "SYSCALL_TEST_WORKER size=%s memory_kib=%s threads_max=%s arch=%s uid=%s\\n" '
         '"$TEST_SIZE" "$ram" "$threads" "$arch" "$(/usr/bin/id -u)"; exec "$@"')
patterns = {"dependency fetch": rb"Error downloading|Failed to fetch|Failed to download",
            "compilation": rb"compilation of rule|C\+\+ compilation of rule|fatal error:",
            "no toolchain": rb"No matching toolchain|no matching toolchains",
            "thread limit": rb"Resource temporarily unavailable|EAGAIN",
            "memory limit": rb"Cannot allocate memory|Out of memory|ENOMEM",
            "setup permission": rb"Operation not permitted|Permission denied",
            "sandbox bootstrap": rb"sandbox exited before the container started",
            "server terminated": rb"Server terminated abruptly",
            "no matching worker": rb"no registered executors|No registered executors|no routable executors",
            "strategy conflict": rb"cannot be executed with any of the available strategies",
            "disk capacity": rb"No space left on device"}
def classes(data):
    return [name for name, regex in patterns.items() if re.search(regex, data)]
def number(value):
    return isinstance(value, int) or isinstance(value, str) and value.isdecimal()
def write():
    result.write_text(json.dumps(receipt, indent=2) + "\n")
def digest(data):
    return hashlib.sha256(data).hexdigest()

def original_output(attempts, name, path, data):
    if len(attempts) != 1:
        return False
    outputs = [o for o in attempts[0].get("testActionOutput", []) if o.get("name") == name]
    if len(outputs) != 1:
        return False
    item, = outputs
    uri = urlparse(item.get("uri", ""))
    cas = re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)$", uri.path)
    if cas:
        return cas[1] == digest(data) and int(cas[2]) == len(data)
    if item.get("digest"):
        return item["digest"] == digest(data) and int(item.get("length", len(data))) == len(data)
    return uri.scheme == "file" and not uri.netloc and Path(unquote(uri.path)).resolve() == path.resolve()

source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert source == os.environ["PRODUCT"]
phase, = sys.argv[1:]
assert phase in ("graph", "test")
bazel = [os.environ["BAZEL"], "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx6g"]
worker = work / "worker"
common = ["--config=x86_64", "--host_platform=//tools/bazeldefs:linux_amd64",
          "--inject_repository=syscall_size_worker=" + str(worker),
          "--extra_execution_platforms=@syscall_size_worker//:amd64",
          "--jobs=32", "--remote_download_outputs=minimal",
          "--remote_download_regex=.*testlogs/.*/test[.](xml|log)$",
          "--spawn_strategy=remote", "--strategy=TestRunner=remote", "--noremote_local_fallback",
          "--modify_execution_info=^TestRunner$=+no-remote-cache", "--test_tag_filters=", "--build_tests_only",
          "--flaky_test_attempts=1", "--nocache_test_results", "--remote_timeout=900", "--test_timeout=900",
          "--keep_going", "--run_under=" + shlex.join(["/bin/bash", "-c", probe, "syscall-worker"])]
if phase == "graph":
    from graph import preflight
    receipt = {"source": source, "size_patch": "e988ba7ba6296bc23d2b8d6bff73d1a10bb9fd6a",
               "integration_parent": "f6d6788e84731673a155c9fff6f28b8624e34c50",
               "status": "INCOMPLETE", "targets": {}}
    write()
    def checkpoint(graph):
        receipt["graph"] = graph
        write()
    try:
        receipt["graph"] = preflight(work, bazel, common, {prefix + n: v[0] for n, v in targets.items()}, checkpoint)
    except (AssertionError, KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        receipt["graph_error"] = type(error).__name__
        write()
        raise
    write()
    raise SystemExit(not receipt["graph"]["qualified"])

receipt = json.loads(result.read_text())
assert receipt["source"] == source and receipt["graph"]["qualified"]
invocation = str(uuid.uuid4())
receipt["invocation"] = invocation
write()
bazel.append("--bazelrc=" + str(work / "remote.bazelrc"))
options = ["--build_event_json_file=" + str(work / "events.json"), "--invocation_id=" + invocation]
start = time.monotonic()
with (work / "console").open("wb") as log:
    try:
        code = subprocess.run([*bazel, "test", *common, *options, *(prefix + name for name in targets)],
                              stdout=log, stderr=subprocess.STDOUT, timeout=45 * 60).returncode
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
receipt["finished"] = [e["finished"].get("exitCode", {}) for e in events if "finished" in e]
receipt["failed_actions"] = []
for event in events:
    action = event.get("action", {})
    if action and not action.get("success") and len(receipt["failed_actions"]) < 12:
        failed_owner = event["id"].get("actionCompleted", {}).get("label", "")
        mnemonic = action.get("type", "")
        receipt["failed_actions"].append({"owner": failed_owner if re.fullmatch(r"//[\w/+.:-]+", failed_owner) else "dependency",
                                          "mnemonic": mnemonic if re.fullmatch(r"\w{1,80}", mnemonic) else "unknown"})
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
        row[output] = {"sha256": digest(data), "bytes": len(data), "original": original_output(attempts, output, path, data)}
        if output == "test.log":
            row["log_classes"] = classes(data)
            witnesses = re.findall(rb"^SYSCALL_TEST_WORKER size=(\w+) memory_kib=(\d+) threads_max=(\d+) arch=(x86_64) uid=(0)$", data, re.M)
            row["workers"] = [{"size": s.decode(), "memory_kib": int(m), "threads_max": int(t), "arch": arch.decode(), "uid": int(uid)}
                              for s, m, t, arch, uid in witnesses if s in (b"large", b"enormous", b"medium", b"small")]
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
          and all(row.get(output, {}).get("original") for output in ("test.log", "test.xml"))
          and row.get("all_cases") and len(row.get("workers", [])) == 1 and row["workers"][0]["size"] == size):
        row["status"] = "PASS"
    write()
if (receipt["started"] == [invocation] and len(receipt["finished"]) == 1 and not receipt.get("incomplete_bep")
        and code == 0 and all(r["status"] == "PASS" for r in receipt["targets"].values())):
    receipt["status"] = "PASS"
elif any(r["status"] == "FAIL" for r in receipt["targets"].values()):
    receipt["status"] = "FAIL"
write()
print(json.dumps({k: v["status"] for k, v in receipt["targets"].items()}))
raise SystemExit(receipt["status"] != "PASS")
