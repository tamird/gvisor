#!/usr/bin/env python3
"""Run the same private netlink/TCP fixture under Linux or a staged Sentry."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

FILES = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "test/rtnetlink/linux"
WORK = Path(os.environ["TEST_TMPDIR"]).resolve()
BIN_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
ENV = {"PATH": BIN_PATH, "LC_ALL": "C"}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run_cell(runtime, variant):
    work = WORK / variant
    root = work / "rootfs"
    root.mkdir(parents=True)
    for name in ("usr", "tmp", "run", "var", "etc", "proc", "sys", "dev", "fixture"):
        (root / name).mkdir()
    for name in ("bin", "sbin", "lib", "lib64"):
        (root / name).symlink_to("usr/" + name)
    (root / "var/run").symlink_to("../run")
    fixture = root / "fixture" / "veth_wire.py"
    shutil.copyfile(FILES / "veth_wire.py", fixture)
    # No customer, host home, workspace, or network is mounted in the sandbox.
    mounts = [dict(destination="/usr", type="bind", source="/usr", options=["rbind", "ro", "nosuid", "nodev"])]
    for destination, kind in (("/proc", "proc"), ("/sys", "sysfs"), ("/tmp", "tmpfs"), ("/run", "tmpfs")):
        mounts.append(dict(destination=destination, type=kind, source=kind))
    caps = ["CAP_SYS_ADMIN", "CAP_NET_ADMIN", "CAP_NET_RAW", "CAP_DAC_OVERRIDE"]
    env = ["PATH=" + BIN_PATH, "LC_ALL=C", "TEST_ON_GVISOR=1"]
    args = ["/usr/bin/python3", "/fixture/veth_wire.py"]
    spec = dict(ociVersion="1.0.2", root=dict(path="rootfs", readonly=True),
                process=dict(args=args, cwd="/", user=dict(uid=0, gid=0),
                             env=env, capabilities={k: caps for k in ("effective", "permitted", "bounding")}),
                mounts=mounts, linux=dict(namespaces=[dict(type=k) for k in ("pid", "network", "ipc", "uts", "mount")]))
    (work / "config.json").write_text(json.dumps(spec))
    # Only the sandbox launcher gets sudo, and only package binaries verified below can be run.
    # A short, private tmpfs keeps runsc's Unix socket below the sockaddr limit
    # and never leaves root-owned files in Bazel's output directories.
    with tempfile.TemporaryDirectory(prefix="veth-mtu-", dir="/tmp") as state:
        args = ["/usr/bin/sudo", "-n", "/usr/bin/unshare", "--mount", "--net", "--propagation", "private",
                "/bin/bash", "-ec", 'mount -t tmpfs -o mode=0700 tmpfs "$1"; shift; exec "$@"', "bridge", state,
                "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LC_ALL=C", runtime,
                "--root=" + state, "--platform=systrap", "--network=none", "--net-raw=true",
                "--ignore-cgroups=true", "run", "--bundle=" + str(work), "vethwire"]
        p = subprocess.run(args, text=True, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=100)
        print(p.stdout, end="", flush=True)
        return p.returncode


name = os.environ["VETH_MODE"]
print("VETH_FIXTURE " + digest(FILES / "veth_wire.py"), flush=True)
if name == "linux":
    # Everything the native iproute2 fixture creates is private to this command.
    cmd = ["/usr/bin/sudo", "-n", "/usr/bin/unshare", "--mount", "--net", "--propagation", "private",
           "/bin/bash", "-ec", 'mount -t tmpfs tmpfs /run; exec "$@"', "vethwire",
           "/usr/bin/env", "-i", "PATH=" + BIN_PATH, "LC_ALL=C", "/usr/bin/python3", str((FILES / "veth_wire.py").resolve())]
    raise SystemExit(subprocess.run(cmd, env=ENV, timeout=100).returncode)
assert name in ("stock", "fixed")
runtime_tree = WORK / "release"
shutil.copytree(FILES / "veth-release", runtime_tree)
runtime = runtime_tree / "runsc"
for path in (runtime, runtime_tree / "gvisor-bin/gvisor_sentry"):
    print("VETH_BINARY " + path.name + " " + digest(path), flush=True)
raise SystemExit(run_cell(runtime, name))
