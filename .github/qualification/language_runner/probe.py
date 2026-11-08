#!/usr/bin/python3
"""Observe the selected Bazel test after setup, before its actual entry point."""

import ast
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def probe(argv, masked):
    program = Path(argv[0])
    raw = program.read_bytes()
    marker = {"masked": masked, "uid": os.geteuid(), "bash": Path("/bin/bash").exists(),
              "entry_sha256": hashlib.sha256(raw).hexdigest(), "argv": argv[1:],
              "format": "ELF" if raw[:4] == b"\x7fELF" else raw.splitlines()[0].decode()}
    assert marker["bash"] != masked and marker["uid"] > 0 and ".runfiles/" in str(Path.cwd())
    if marker["format"] == "ELF":
        assert raw[4:6] == b"\x02\x01" and int.from_bytes(raw[18:20], "little") == 62
    proctors = [p for p in Path("test/runtimes/proctor").glob("**/proctor_bin") if p.is_file()]
    assert len(proctors) == 1, proctors
    marker["proctor"] = [str(proctors[0]), digest(proctors[0])]
    release = Path("release")
    marker["release"] = {str(p.relative_to(release)): digest(p) for p in release.rglob("*") if p.is_file()}
    if not masked and marker["format"] != "ELF":
        trace = Path(os.environ["TEST_TMPDIR"]) / "executed"
        status = subprocess.run(["/usr/bin/strace", "-f", "-qq", "-s", "4096", "-e", "trace=execve",
                                 "-o", str(trace), *argv]).returncode
        marker["go_argv"] = []
        for line in trace.read_text().splitlines():
            match = re.search(r'execve\("([^"]*)", (\[.*?\]), .*\)\s+= 0', line)
            if match and "/runner/" in match[1] and Path(match[1]).name == "runner":
                marker["go_argv"].append(ast.literal_eval(match[2])[1:])
    else:
        try:
            status = subprocess.run(argv).returncode
        except FileNotFoundError as e:
            assert masked and e.errno == errno.ENOENT
            marker["errno"] = e.errno
            status = 127
        if marker["format"] == "ELF":
            marker["go_argv"] = [argv[1:]]
    marker["exit"] = status
    print("Q_LANGUAGE " + json.dumps(marker, sort_keys=True), flush=True)
    return status


def main(argv):
    if argv[0] == "--private":
        uid, gid, mountns, netns = map(int, argv[1:5])
        assert os.geteuid() == 0 and min(uid,gid) > 0
        assert os.stat("/proc/self/ns/mnt").st_ino != mountns and os.stat("/proc/self/ns/net").st_ino != netns
        libc = ctypes.CDLL(None,use_errno=True)
        for bind in {Path("/bin").resolve(),Path("/usr/bin").resolve()}:
            if libc.mount(b"tmpfs", os.fsencode(bind), b"tmpfs", 14, b"mode=0555,size=4096"):
                raise OSError(ctypes.get_errno(), "mount empty binary directory", str(bind))
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
        return probe(argv[5:], True)
    if os.environ["LANGUAGE_MASK"] == "1":
        return subprocess.run(["/usr/bin/sudo","-n","/usr/bin/unshare","--mount","--net","--propagation","private",
                               "/usr/bin/env","-i","PATH=/usr/bin:/bin","/usr/bin/python3","-IB",str(Path(__file__).resolve()),
                               "--private",str(os.getuid()),str(os.getgid()),str(os.stat("/proc/self/ns/mnt").st_ino),
                               str(os.stat("/proc/self/ns/net").st_ino),*argv]).returncode
    return probe(argv, False)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
