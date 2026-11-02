"""Diagnose one native ARM64 owner without changing the machine's userns policy."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time

import qualification_report as report


TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "unit-results"
OWNER = "//sandboxexec/sandbox/python:sandbox_py_test"
CASE = "SandboxTest.test_exec_with_args"
SIGNATURE = "cannot create gofer process: gofer: fork/exec /proc/self/exe: permission denied"
RESULT = {"status": "INCOMPLETE", "arms": {}}


def save():
    report.write_json(OUT / "verdict.json", RESULT)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def host():
    values = {name: (Path("/proc/sys/kernel") / name).read_text().strip()
              for name in ("apparmor_restrict_unprivileged_userns", "unprivileged_userns_clone")}
    values.update(arch=platform.machine(), kernel=platform.release(), uid=os.getuid(),
                  cgroup_v2=Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
                  security=[line for line in Path("/proc/self/status").read_text().splitlines()
                            if line.startswith(("CapEff:", "Seccomp:", "NoNewPrivs:"))],
                  label=Path("/proc/self/attr/current").read_text().strip())
    return values


def audit(start):
    command = ["sudo", "-n", "journalctl", "-k", "-b", "--no-pager", "-o", "cat", "--since", f"@{start}"]
    proc = subprocess.run(command, capture_output=True, text=True)
    rows = []
    for line in proc.stdout.splitlines():
        if 'apparmor=' not in line:
            continue
        timestamp = re.search(r"\baudit\(([0-9]+\.[0-9]+):", line)
        fields = {"time": float(timestamp[1]) if timestamp else None}
        for key in ("apparmor", "operation", "class", "profile", "comm", "pid", "error",
                    "capname", "requested_mask", "denied_mask", "target"):
            match = re.search(r'\b' + key + r'=(?:"([^"]+)"|([^ ]+))', line)
            if match:
                value = match[1] or match[2]
                if re.fullmatch(r"[A-Za-z0-9._:/()+ =-]{1,128}", value):
                    fields[key] = value
        if fields.get("comm") in ("runsc", "exe", "gofer") and fields.get("apparmor") in ("DENIED", "AUDIT"):
            rows.append(fields)
    return {"reader_exit": proc.returncode, "rows": rows[:48], "total": len(rows)}


def trace_events(directory):
    found = []
    for path in sorted(directory.glob("syscall.*")):
        tid = path.name.split(".")[-1]
        for line in path.read_text(errors="replace").splitlines():
            call = re.match(r"(\d+\.\d+) ([a-zA-Z0-9_]+)\(", line)
            if not call:
                continue
            error = re.search(r"= -1 (EACCES|EPERM)\b", line)
            flags = sorted(set(re.findall(r"\bCLONE_NEW[A-Z]+\b", line)))
            self_exec = call[2] in ("execve", "execveat") and '"/proc/self/exe"' in line
            runsc = call[2] == "execve" and re.search(r'execve\("[^"]*/release/runsc"', line) is not None
            if flags or self_exec or runsc or error:
                duration = re.search(r"<(\d+\.\d+)>$", line)
                found.append({"tid": int(tid), "time": float(call[1]), "duration": float(duration[1]) if duration else 0,
                              "syscall": call[2], "namespace_flags": flags,
                              "error": error[1] if error else None, "self_exec": self_exec,
                              "runsc_exec": runsc, "success": bool(re.search(r"= [0-9]+ <", line))})
    return {"count": len(found), "events": found[:100]}


def arm(name, source, flags, trace_dir=None):
    row = RESULT["arms"][name] = {"source": source, "stage": "setup"}
    save()
    subprocess.run(["git", "checkout", "--quiet", "--detach", source], check=True)
    assert output("git", "rev-parse", "HEAD") == source
    assert not output("git", "status", "--porcelain", "--untracked-files=no")
    directory = TMP / ("case-" + name)
    directory.mkdir()
    events = directory / "raw.jsonl"
    command = ["bazel", f"--bazelrc={TMP / 'unit-resources.bazelrc'}", "test", *flags,
               "--test_env=GO_TEST_WRAP_TESTV=true", "--nocache_test_results",
               "--flaky_test_attempts=1", "--runs_per_test=1", "--test_arg=-v", f"--test_arg={CASE}",
               "--nobuild_event_json_file_path_conversion", f"--build_event_json_file={events}"]
    if trace_dir:
        tool = shutil.which("strace")
        assert tool, "strace is unavailable"
        command.append(f"--run_under={tool} -ff -qq -ttt -T -s 180 -e trace=%process,unshare,setns,setgroups,setuid,setgid,setresuid,setresgid,capset,prctl,mount -o {trace_dir}/syscall")
    if name == "stock":
        command.extend(["--", OWNER])
    start = int(time.time())
    row.update(argv=command, started=start)
    save()
    console = directory / "console"
    with console.open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    row.update(exit=result.returncode, finished=time.time(), stage="Bazel finished")
    save()
    if result.returncode:
        lines = console.read_text(errors="replace").splitlines()
        diagnostic = next((line for line in lines if re.search(r"^ERROR:|^FATAL:", line)), "")
        row["first_bazel_error"] = diagnostic[:460]
        save()
    capture = report.capture(events, directory / "events.jsonl", Path("bazel-out"), directory / "reports")
    assert not capture["errors"], capture["errors"]
    observed = [e for line in events.read_text().splitlines() if "testResult" in (e := json.loads(line)).get("id", {})]
    assert len(observed) == 1, f"{len(observed)} TestResults"
    event, = observed
    identity = event["id"]["testResult"]
    payload = event["testResult"]
    row.update(attempt=identity, result=payload["status"], cached=payload.get("cachedLocally", False),
               strategy=payload.get("executionInfo", {}).get("strategy"))
    assert identity["label"] == OWNER and all(identity[k] == 1 for k in ("run", "shard", "attempt"))
    assert not row["cached"] and not payload.get("executionInfo", {}).get("cachedRemotely")
    for ext in ("xml", "log"):
        original, = (directory / "reports").rglob("test." + ext)
        data = original.read_bytes()
        (OUT / (name + "." + ext)).write_bytes(data)
        row[ext + "_sha256"] = sha(data)
    log = (OUT / (name + ".log")).read_text()
    assert "test_exec_with_args (" in log and re.search(r"Ran 1 test in ", log) and "skipped=" not in log
    if result.returncode == 3 and payload["status"] == "FAILED" and SIGNATURE in log and "FAILED (errors=1)" in log:
        row["outcome"] = "GOFER_EACCES"
    elif result.returncode == 0 and payload["status"] == "PASSED" and re.search(r"^OK$", log, re.M):
        row["outcome"] = "ECHO_DELIVERED"
    else:
        row["outcome"] = "OTHER"
    row["audit"] = audit(int(payload["testAttemptStartMillisEpoch"]) // 1000)
    if trace_dir:
        row["trace"] = trace_events(trace_dir)
    sandbox_root = Path("bazel-bin/sandboxexec/sandbox/python/sandbox_py_test.runfiles/_main/release/runsc")
    if sandbox_root.exists():
        executable = sandbox_root.read_bytes()
        assert executable[:4] == b"\x7fELF" and int.from_bytes(executable[18:20], "little") == 183
        row["runsc_sha256"] = sha(executable)
    row["stage"] = "complete"
    save()


def main():
    machine = RESULT["before"] = host()
    assert machine["arch"] == "aarch64" and machine["uid"] != 0 and machine["cgroup_v2"]
    assert os.environ["CGROUPV2"] == "true" and machine["apparmor_restrict_unprivileged_userns"] == "1"
    ownerfile = TMP / "one-owner.txt"
    ownerfile.write_text(OWNER + "\n")
    stock = ["--strip=never", "--config=ci", "--sandbox_add_mount_pair=/tmp",
             "--test_tag_filters=-nogo,-requires-kvm", "--build_tag_filters=-network_plugins", "--test_env=CGROUPV2=true"]
    candidate = ["--config=buildkite-unit", f"--target_pattern_file={ownerfile}"]
    for name, source, flags in [("stock", os.environ["STOCK"], stock), ("candidate", os.environ["PRODUCT"], candidate)]:
        try:
            arm(name, source, flags)
        except Exception as error:
            RESULT["arms"][name]["problem"] = str(error)[:500]
            save()
    if RESULT["arms"]["candidate"].get("outcome") == "GOFER_EACCES":
        try:
            with tempfile.TemporaryDirectory(prefix="sandbox-python-trace-", dir="/tmp") as trace_dir:
                arm("trace", os.environ["PRODUCT"], candidate, Path(trace_dir))
        except Exception as error:
            RESULT["arms"]["trace"]["problem"] = str(error)[:500]
    RESULT["after"] = host()
    arms = RESULT["arms"]
    if all(arms[k].get("outcome") in ("GOFER_EACCES", "ECHO_DELIVERED") for k in ("stock", "candidate")):
        RESULT["status"] = "TWO_OBSERVED_OWNERS"
        traced = arms.get("trace", {})
        if all(arms.get(k, {}).get("outcome") == "GOFER_EACCES" for k in ("stock", "candidate", "trace")):
            denied = [e for e in traced.get("trace", {}).get("events", [])
                      if e["error"] == "EACCES" and e["namespace_flags"]]
            matched = []
            for record in traced.get("audit", {}).get("rows", []):
                if record.get("apparmor") != "DENIED" or not record.get("time"):
                    continue
                if not (record.get("operation") == "userns_create" or
                        (record.get("operation") == "capable" and record.get("profile") == "unprivileged_userns")):
                    continue
                for event in denied:
                    if (str(event["tid"]) == record.get("pid") and
                        event["time"] - 0.005 <= record["time"] <= event["time"] + event["duration"] + 0.005):
                        matched.append({"audit": record, "syscall": event})
            if matched:
                RESULT["status"] = "HOST_APPARMOR_DENIAL_MATCHED_TO_NAMESPACE_SYSCALL"
                RESULT["matched_audit"] = matched
    assert RESULT["before"] == RESULT["after"]
    RESULT["verified_at"] = time.time()
    save()
    assert RESULT["status"] != "INCOMPLETE", RESULT["status"]


if __name__ == "__main__":
    main()
