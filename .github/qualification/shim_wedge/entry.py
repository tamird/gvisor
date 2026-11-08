#!/usr/bin/python3
"""Run the original Bazel-selected Go test after its Unix test setup."""

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def child(argv: list[str], mode: str) -> int:
    program = Path(argv[0]).resolve(strict=True)
    data = program.read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == 62
    masked = mode != "ordinary"
    marker = {
        "mode": mode, "uid": os.geteuid(), "program": program.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "sh": Path("/bin/sh").exists(), "sleep": Path("/usr/bin/sleep").exists(),
    }
    assert marker["uid"] != 0 and marker["sh"] != masked and marker["sleep"] != masked
    trace = mode == "trace"
    if trace:
        prefix = Path(os.environ["TEST_TMPDIR"]) / "shim-wedge.trace"
        tracer = Path(__file__).resolve().with_name("strace")
        status = subprocess.run([str(tracer), "-ff", "-q", "--seccomp-bpf", "-s", "360",
                                 "-e", "trace=execve,waitid,wait4,kill,pidfd_send_signal",
                                 "-o", str(prefix), *argv]).returncode
        traces = {int(p.suffix[1:]): p.read_text(errors="replace")
                  for p in prefix.parent.glob(prefix.name + ".*")}
        children = []
        for pid, text in traces.items():
            for path, result in re.findall(r'^execve\("([^"]+)",[^\n]+\)\s+=\s+(0|-1 ENOENT)', text, re.M):
                if (name := Path(path).name) not in {"wedged-runsc", "fake-runsc"}:
                    continue
                waits = [line.strip()[:360] for s in traces.values() for line in s.splitlines()
                         if (line.startswith("waitid(") and f"si_pid={pid}," in line and
                             "CLD_KILLED" in line and "SIGKILL" in line) or
                            (line.startswith("wait4(") and line.endswith("= " + str(pid))
                             and "SIGKILL" in line)]
                children.append({"pid": pid, "program": name, "exec": result, "waits": waits[:3],
                                 "killed": "+++ killed by SIGKILL +++" in text})
        marker["children"] = children
    else:
        status = subprocess.run(argv).returncode
    marker["exit"] = status
    print("Q_SHIM " + json.dumps(marker, sort_keys=True), flush=True)
    return status


def main(args: list[str]) -> int:
    if args[0] == "--inside":
        mode = args[1]
        uid, gid, mountns, netns = map(int, args[2:6])
        assert os.geteuid() == 0 and min(uid, gid) > 0
        assert os.stat("/proc/self/ns/mnt").st_ino != mountns
        assert os.stat("/proc/self/ns/net").st_ino != netns
        libc = ctypes.CDLL(None, use_errno=True)
        for dest in {Path("/bin").resolve(), Path("/usr/bin").resolve()}:
            if libc.mount(b"tmpfs", os.fsencode(dest), b"tmpfs", 14, b"mode=0555,size=4096"):
                raise OSError(ctypes.get_errno(), "mount empty binary directory", str(dest))
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
        if libc.prctl(38, 1, 0, 0, 0):
            raise OSError(ctypes.get_errno(), "set no_new_privs")
        return child(args[6:], mode)
    mode = os.environ["SHIM_QUAL_MODE"]
    if mode == "ordinary":
        return child(args, mode)
    assert mode in {"masked", "trace"}
    allowed = ["TEST_TMPDIR", "TEST_SRCDIR", "TEST_WORKSPACE", "XML_OUTPUT_FILE", "TEST_TARGET",
               "RUNFILES_DIR", "RUNFILES_MANIFEST_FILE", "TEST_UNDECLARED_OUTPUTS_DIR",
               "TEST_BINARY", "TESTBRIDGE_TEST_ONLY", "GO_TEST_WRAP", "GTEST_TMP_DIR"]
    env = [f"{key}={os.environ[key]}" for key in allowed if key in os.environ]
    return subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/unshare", "--mount", "--net",
                           "--propagation", "private", "/usr/bin/env", "-i", "PATH=/usr/bin:/bin",
                           "HOME=" + os.environ["TEST_TMPDIR"], *env, "/usr/bin/python3", "-IB",
                           str(Path(__file__).resolve()), "--inside", mode, str(os.getuid()), str(os.getgid()),
                           str(os.stat("/proc/self/ns/mnt").st_ino), str(os.stat("/proc/self/ns/net").st_ino),
                           *args]).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
