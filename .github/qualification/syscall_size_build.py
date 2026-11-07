"""Locate the first build failure in the original, credential-free native client."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import shlex
import subprocess


SOURCE = "e988ba7ba6296bc23d2b8d6bff73d1a10bb9fd6a"
WORK = Path(os.environ["RUNNER_TEMP"], "syscall-build")
TARGETS = [
    "//test/syscalls:processes_test_runsc_ptrace",
    "//test/syscalls:memory_accounting_test_runsc_systrap_directfs_save",
    "//test/syscalls:memory_accounting_test_runsc_systrap_directfs_save_resume",
]
PROBE = ('while read -r key value unit; do if [[ "$key" == MemTotal: ]]; then ram="$value"; break; fi; '
         'done < /proc/meminfo; read -r threads < /proc/sys/kernel/threads-max; '
         'printf "SYSCALL_TEST_WORKER size=%s memory_kib=%s threads_max=%s\\n" '
         '"$TEST_SIZE" "$ram" "$threads"; exec "$@"')
FLAGS = [
    "--config=x86_64", "--jobs=4", "--remote_download_outputs=all",
    "--spawn_strategy=linux-sandbox", "--strategy=TestRunner=linux-sandbox",
    "--noremote_local_fallback", "--modify_execution_info=^TestRunner$=+no-remote-cache",
    "--test_tag_filters=", "--build_tests_only", "--flaky_test_attempts=1", "--nocache_test_results",
    "--remote_timeout=900", "--test_timeout=900", "--keep_going",
    "--remote_default_exec_properties=Arch=amd64", "--remote_default_exec_properties=OSFamily=linux",
    "--remote_default_exec_properties=workload-isolation-type=firecracker",
    "--remote_default_exec_properties=dockerUser=root",
    "--remote_default_exec_properties=container-image=docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
    "--remote_executor=", "--remote_cache=", "--bes_backend=",
    "--run_under=" + shlex.join(["/bin/bash", "-c", PROBE, "syscall-worker"]),
    "--color=no", "--curses=no", "--noshow_progress",
]
DIAGNOSTIC = re.compile(
    r"^ERROR:|^FATAL:|error:|^Caused by:|^Exception|no such (?:file|target|package)|"
    r"(?:cannot|could not|can't) (?:find|load|open|locate)|undefined reference|"
    r"undefined:|command not found|unrecognized (?:option|command)|"
    r"undeclared inclusion|undeclared identifier|undefined symbol|"
    r"invalid (?:flag|argument)|requires .* (?:compiler|toolchain)|"
    r"incompatible|permission denied|no space left|out of memory|"
    r"no matching|not in std|missing (?:strict )?dependenc|Traceback \(|"
    r"\.(?:c|cc|h|hpp|go|s|bzl):\d+(?::\d+)?:\s+\S|"
    r"^(?:compilepkg|link|ld|collect2|gcc|g\+\+):",
    re.I,
)
SENSITIVE = re.compile(
    r"authorization|remote_header|bes_header|api[-_]key|bearer |github_pat_|"
    r"gh[pousr]_|private key|token=|password=|credential=|signature=", re.I,
)
LABEL = re.compile(r"(?:@{1,2}[\w.+~-]*)?//[\w./+-]*:[\w./+-]+")


def scrub(line):
    line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).strip()
    if SENSITIVE.search(line):
        return ""
    line = line.replace(str(Path.cwd()) + "/", "").replace(os.environ["HOME"] + "/", "~/")
    line = re.sub(r"(?:https?|grpcs?)://\S+", "<URL>", line)
    line = re.sub(r"\b[\w+/=-]{48,}\b", "<long value>", line)
    return "".join(c for c in line if c.isprintable())[:520]


def diagnostic_lines(path):
    lines = []
    with path.open(errors="replace") as stream:
        for n, line in enumerate(stream, 1):
            if DIAGNOSTIC.search(line) and (clean := scrub(line)):
                if all(row["message"] != clean for row in lines):
                    lines.append({"line": n, "message": clean})
            if len(lines) == 24:
                break
    return lines


def phase(name, command, extra, minutes):
    log = WORK / (name + ".log")
    bep = WORK / (name + ".jsonl")
    args = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", command, *FLAGS,
            "--build_event_json_file=" + str(bep), *extra, *TARGETS]
    with log.open("wb") as output:
        try:
            code = subprocess.run(args, stdout=output, stderr=output, timeout=minutes * 60).returncode
        except subprocess.TimeoutExpired:
            code = 124
    report = {"exit": code, "console": diagnostic_lines(log), "roots": {}, "actions": [],
              "aborts": {}, "tests_executed": 0}
    aborts = Counter()
    if bep.exists():
        with bep.open(errors="replace") as events:
            for line in events:
                try:
                    event = json.loads(line)
                except ValueError:
                    report["truncated_bep"] = True
                    break
                ident = event.get("id", {})
                if "testResult" in event:
                    report["tests_executed"] += 1
                if abort := event.get("aborted"):
                    aborts[abort.get("reason", "unknown")] += 1
                target = ident.get("targetCompleted", {}).get("label")
                if target in TARGETS:
                    report["roots"][target] = abort["reason"] if abort else event.get("completed", {}).get("success", False)
                action = event.get("action")
                if action and not action.get("success") and len(report["actions"]) < 12:
                    owner = ident.get("actionCompleted", {}).get("label", "")
                    mnemonic = action.get("type", "")
                    report["actions"].append({"owner": owner if LABEL.fullmatch(owner) else None,
                                              "mnemonic": mnemonic if re.fullmatch(r"\w{1,64}", mnemonic) else None,
                                              "exit": action.get("exitCode")})
                if finish := event.get("finished"):
                    report["finished"] = finish.get("exitCode")
    report["aborts"] = dict(aborts)
    return report


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    report = {"source": SOURCE, "runner": "ubuntu-22.04", "targets": TARGETS}
    for name, command, extra, minutes in [("analysis", "test", ["--nobuild"], 12),
                                         ("compile", "build", [], 44)]:
        report[name] = phase(name, command, extra, minutes)
        (WORK / "diagnostic.json").write_text(json.dumps(report, indent=2) + "\n")
        assert not report[name]["tests_executed"], name
        print(name, report[name]["exit"], "diagnostic lines", len(report[name]["console"]), flush=True)
    assert report["compile"]["exit"] == 0, "native compile failed; inspect the bounded diagnostic"


if __name__ == "__main__":
    main()
