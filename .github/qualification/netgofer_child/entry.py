#!/usr/bin/python3
"""Run the original Bazel-selected Go test after its Unix test setup."""

import ctypes
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
    missing = [p for p in ("/bin/cat", "/bin/sleep", "/usr/bin/cat", "/usr/bin/sleep") if not Path(p).exists()]
    assert len(missing) == 4 and os.geteuid() != 0 and mode == "trace"
    prefix = Path(os.environ["TEST_TMPDIR"]) / "netgofer.trace"
    tracer = Path(__file__).resolve().with_name("strace")
    status = subprocess.run([str(tracer), "-ff", "-q", "--seccomp-bpf", "-s", "240",
                             "-e", "trace=execve,waitid,wait4,kill",
                             "-o", str(prefix), *argv]).returncode
    traces = {int(p.suffix[1:]): p.read_text(errors="replace").splitlines()
              for p in prefix.parent.glob(prefix.name + ".*")}
    children = []
    for pid, lines in traces.items():
        for line in lines:
            m = re.match(r'^execve\("(/bin/cat|/bin/sleep|/proc/self/exe)", \["([^"]+)"[^\n]+\)\s+=\s+(0|-1 ENOENT)', line)
            if m is None:
                continue
            waits = [s.strip()[:400] for thread in traces.values() for s in thread
                     if (s.startswith(f"waitid(P_PID, {pid},") and f"si_pid={pid}," in s)
                     or (s.startswith(f"wait4({pid},") and s.endswith("= " + str(pid)))
                     or re.match(rf'^kill\({pid}, (?:SIGKILL|0)\)\s+= ', s)]
            children.append({"pid": pid, "path": m[1], "argv0": m[2], "exec": m[3], "waits": waits})
    marker = {"missing": missing, "uid": os.geteuid(), "program": program.name, "argv": argv[1:],
              "elf_sha256": hashlib.sha256(data).hexdigest(), "exit": status, "children": children}
    print("Q_NETGOFER " + json.dumps(marker, sort_keys=True), flush=True)
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
    mode = "trace"
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
