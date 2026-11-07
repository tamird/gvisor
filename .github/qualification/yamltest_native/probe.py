#!/usr/bin/python3
"""Run the Bazel-selected test in its original runfiles directory."""

import ctypes
import errno
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path


def run(masked, argv):
    program = Path(argv[0])
    raw = program.read_bytes()
    report = {
        "masked": masked,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "format": "ELF" if raw.startswith(b"\x7fELF") else raw.split(b"\n", 1)[0].decode(),
        "cpu": platform.machine(),
        "uid": os.geteuid(),
        "args": argv[1:],
        "bash_exists": Path("/bin/bash").exists(),
        "cwd_is_runfiles": ".runfiles/" in os.getcwd(),
    }
    assert report["cpu"] == "x86_64" and report["uid"] > 0 and report["cwd_is_runfiles"]
    assert report["bash_exists"] != masked
    if report["format"] == "ELF":
        assert raw[4:6] == b"\x02\x01" and int.from_bytes(raw[18:20], "little") == 62
    try:
        result = subprocess.run(argv, check=False).returncode
    except OSError as error:
        report["exec_errno"] = error.errno
        if error.errno != errno.ENOENT:
            raise
        result = 127
    report["exit"] = result
    print("YAMLTEST_PROBE " + json.dumps(report, sort_keys=True), flush=True)
    return result


def main(argv):
    if argv[0] == "--mask":
        uid, gid = map(int, argv[1:3])
        env = json.loads(argv[3])
        assert os.geteuid() == 0 and uid > 0 and gid > 0
        assert os.stat("/proc/self/ns/mnt").st_ino != int(argv[4])
        assert os.stat("/proc/self/ns/net").st_ino != int(argv[5])
        assert Path("/bin/bash").is_file()
        libc = ctypes.CDLL(None, use_errno=True)
        for path in {Path("/bin").resolve(), Path("/usr/bin").resolve()}:
            if libc.mount(b"tmpfs", os.fsencode(path), b"tmpfs", 14, b"mode=0555,size=4096"):
                raise OSError(ctypes.get_errno(), "mount empty binary directory", path)
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
        os.environ.clear()
        os.environ.update(env)
        return run(True, argv[6:])
    if os.environ["YAMLTEST_MASK"] == "1":
        env = {k: v for k, v in os.environ.items() if k.startswith(("TEST_", "RUNFILES_")) or k in {"PATH", "LANG", "TMPDIR"}}
        return subprocess.run([
            "/usr/bin/sudo", "-n", "/usr/bin/unshare", "--mount", "--net", "--propagation", "private",
            "--", "/usr/bin/python3", "-IB", str(Path(__file__).resolve()), "--mask",
            str(os.getuid()), str(os.getgid()), json.dumps(env),
            str(os.stat("/proc/self/ns/mnt").st_ino), str(os.stat("/proc/self/ns/net").st_ino), *argv,
        ], check=False).returncode
    return run(False, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
