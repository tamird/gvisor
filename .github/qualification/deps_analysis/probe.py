#!/usr/bin/python3
"""Run only the selected Bazel test child in a private filesystem and network."""

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def child(argv, mode):
    program = Path(argv[0])
    raw = program.read_bytes()
    row = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "format": "ELF" if raw.startswith(b"\x7fELF") else raw.split(b"\n", 1)[0].decode(),
        "sh": Path("/bin/sh").is_file(),
        "bash": Path("/bin/bash").is_file(),
        "mode": mode,
        "uid": os.geteuid(),
    }
    assert row["uid"] > 0 and ".runfiles/" in os.getcwd()
    assert (row["sh"], row["bash"]) == {"host": (True, True), "sh-only": (True, False), "empty": (False, False)}[mode]
    if row["format"] == "ELF":
        assert raw[4:6] == b"\x02\x01" and int.from_bytes(raw[18:20], "little") == 62
    try:
        rc = subprocess.run(argv, check=False).returncode
    except FileNotFoundError as error:
        assert error.errno == errno.ENOENT and program.exists()
        row["exec_errno"] = error.errno
        rc = 127
    row["exit"] = rc
    print("DEPS_TEST_PROBE " + json.dumps(row, sort_keys=True), flush=True)
    return rc


def main(argv):
    if argv[0] == "--private":
        mode, uid, gid, env, mount_ns, net_ns, *command = argv[1:]
        assert os.geteuid() == 0 and int(uid) > 0 and int(gid) > 0
        assert os.stat("/proc/self/ns/mnt").st_ino != int(mount_ns)
        assert os.stat("/proc/self/ns/net").st_ino != int(net_ns)
        # Preserve only the ordinary POSIX interpreter for the sh-only control.
        shell = Path("/bin/dash").read_bytes() if mode == "sh-only" else None
        libc = ctypes.CDLL(None, use_errno=True)
        for path in {Path("/bin").resolve(), Path("/usr/bin").resolve()}:
            if libc.mount(b"tmpfs", os.fsencode(path), b"tmpfs", 6, b"mode=0755,size=2m"):
                raise OSError(ctypes.get_errno(), "mount private program directory", path)
        if shell is not None:
            Path("/bin/sh").write_bytes(shell)
            Path("/bin/sh").chmod(0o555)
        os.setgroups([])
        os.setgid(int(gid))
        os.setuid(int(uid))
        os.environ.clear()
        os.environ.update(json.loads(env))
        return child(command, mode)
    mode = os.environ["TEST_DEPS_MODE"]
    if mode == "host":
        return child(argv, mode)
    assert mode in {"sh-only", "empty"}
    env = {key: value for key, value in os.environ.items()
           if key.startswith(("TEST_", "RUNFILES_")) or key in {"PATH", "LANG", "TMPDIR"}}
    return subprocess.run([
        "/usr/bin/sudo", "-n", "/usr/bin/unshare", "--mount", "--net", "--propagation", "private", "--",
        "/usr/bin/python3", "-IB", str(Path(__file__).resolve()), "--private", mode,
        str(os.getuid()), str(os.getgid()), json.dumps(env), str(os.stat("/proc/self/ns/mnt").st_ino),
        str(os.stat("/proc/self/ns/net").st_ino), *argv,
    ], check=False).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
