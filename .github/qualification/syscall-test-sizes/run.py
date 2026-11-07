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
         'printf "SYSCALL_TEST_WORKER size=%s memory_kib=%s threads_max=%s arch=%s\\n" '
         '"$TEST_SIZE" "$ram" "$threads" "$arch"; exec "$@"')
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

def compile_diagnostics(log):
    lines = []
    for index, raw in enumerate(log.open(errors="replace"), 1):
        if not re.search(r"^ERROR:|^FATAL:|\berror:|undefined reference|undefined:|cannot find|"
                         r"no such file|No space left|command not found|\.(?:go|cc|h|bzl):\d+:", raw, re.I):
            continue
        if re.search(r"api[-_]key|authorization|header=|bearer |token=|password=|private key", raw, re.I):
            continue
        clean = raw.strip().replace(str(root) + "/", "").replace(os.environ["HOME"] + "/", "~/")
        clean = re.sub(r"(?:https?|grpcs?)://\S+|\b[\w+/=-]{48,}\b", "<omitted>", clean)
        lines.append({"line": index, "message": "".join(c for c in clean if c.isprintable())[:500]})
        if len(lines) == 14:
            break
    return lines

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
assert phase in ("compile", "test")
bazel = [os.environ["BAZEL"], "--nosystem_rc", "--nohome_rc"]
common = ["--config=x86_64", "--jobs=4", "--remote_download_outputs=all",
          "--spawn_strategy=linux-sandbox", "--strategy=TestRunner=remote", "--noremote_local_fallback",
          "--modify_execution_info=^TestRunner$=+no-remote-cache", "--test_tag_filters=", "--build_tests_only", "--flaky_test_attempts=1", "--nocache_test_results",
          "--remote_timeout=900", "--test_timeout=900", "--keep_going"]
common.extend("--remote_default_exec_properties=" + p for p in (
    "Arch=amd64", "OSFamily=linux", "workload-isolation-type=firecracker", "dockerUser=root",
    "container-image=docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"))
common.append("--run_under=" + shlex.join(["/bin/bash", "-c", probe, "syscall-worker"]))
nogos = ("GoStandardLibraryAnalysis", "GoStaticAnalysis")
if phase == "compile":
    assert not (work / "remote.bazelrc").exists()
    report = {"nogo_strategy": "local", "roots": {}, "nogo_actions": {}, "failures": [], "tests_executed": 0}
    receipt = {"source": source, "status": "INCOMPLETE", "targets": {}, "compile": report}
    write()
    bep = work / "compile.bep"
    log = work / "compile.log"
    options = ["--remote_executor=", "--remote_cache=", "--bes_backend=", "--strategy=TestRunner=linux-sandbox",
               "--build_event_json_file=" + str(bep), "--build_event_publish_all_actions",
               *("--strategy=" + m + "=local" for m in nogos)]
    with log.open("wb") as out:
        try:
            report["exit"] = subprocess.run([*bazel, "build", *common, *options, *(prefix + n for n in targets)],
                                            stdout=out, stderr=out, timeout=60*60).returncode
        except subprocess.TimeoutExpired:
            report["exit"] = 124
    report["diagnostics"] = compile_diagnostics(log)
    if bep.is_file():
        for line in bep.open():
            try:
                e = json.loads(line)
            except ValueError:
                report["truncated_bep"] = True
                break
            ident = e["id"]
            label = ident.get("targetCompleted", {}).get("label", "").removeprefix(prefix)
            if label in targets and "completed" in e:
                report["roots"][label] = bool(e["completed"].get("success"))
            if "testResult" in e:
                report["tests_executed"] += 1
            action = e.get("action", {})
            kind = action.get("type", "")
            if kind in nogos and action.get("success"):
                report["nogo_actions"][kind] = report["nogo_actions"].get(kind, 0) + 1
            if action and not action.get("success") and len(report["failures"]) < 12:
                owner = ident.get("actionCompleted", {}).get("label", "")
                report["failures"].append({"owner": owner if re.fullmatch(r"//[\w/+.:-]+", owner) else "dependency",
                                           "mnemonic": kind if re.fullmatch(r"\w{1,80}", kind) else "unknown"})
    report["qualified"] = (report["exit"] == 0 and report["roots"] == dict.fromkeys(targets, True)
                           and not report["tests_executed"] and not report.get("truncated_bep")
                           and set(report["nogo_actions"]) == set(nogos))
    write()
    raise SystemExit(not report["qualified"])

receipt = json.loads(result.read_text())
assert receipt["source"] == source and receipt["compile"]["qualified"]
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
            witnesses = re.findall(rb"^SYSCALL_TEST_WORKER size=(\w+) memory_kib=(\d+) threads_max=(\d+) arch=(x86_64)$", data, re.M)
            row["workers"] = [{"size": s.decode(), "memory_kib": int(m), "threads_max": int(t), "arch": arch.decode()}
                              for s, m, t, arch in witnesses if s in (b"large", b"enormous", b"medium", b"small")]
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
