#!/usr/bin/env python3
"""Run the existing bridge GTest using the qualified source-OCI isolation."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

FILES = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "test/rtnetlink/linux"
WORK = Path(os.environ["TEST_TMPDIR"]).resolve()
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
FILTER = "--gtest_filter=NetlinkRouteTest.BridgeAddUnnamed"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    mode = os.environ["BRIDGE_MODE"]
    assert mode in ("native", "stock", "fixed"), mode
    fixture = WORK / "bridge-test"
    shutil.copy2(FILES / "bridge-fixture", fixture)
    print("BRIDGE_BINARY fixture " + digest(fixture), flush=True)
    output = WORK / "results"
    output.mkdir()
    xml = output / "test.xml"
    xml.touch(mode=0o600)  # Keep the XML owned by the unprivileged test user.
    launcher = ["/usr/bin/sudo", "-n", "/usr/bin/unshare", "--mount", "--net", "--propagation", "private"]
    if mode == "native":
        command = [*launcher, "/usr/bin/env", "-i", *[f"{k}={v}" for k, v in ENV.items()],
                   str(fixture), FILTER, "--gtest_output=xml:" + str(xml)]
        try:
            result = subprocess.run(command, env=ENV, timeout=180)
            return result.returncode
        finally:
            if xml.is_file() and xml.stat().st_size:
                shutil.copyfile(xml, os.environ["XML_OUTPUT_FILE"])

    runtime_tree = WORK / "release"
    shutil.copytree(FILES / "bridge-release", runtime_tree)
    runtime = runtime_tree / "runsc"
    for name, path in (("runsc", runtime), ("gvisor_sentry", runtime_tree / "gvisor-bin/gvisor_sentry")):
        print("BRIDGE_BINARY " + name + " " + digest(path), flush=True)
    work = WORK / "bundle"
    root = work / "rootfs"
    root.mkdir(parents=True)
    for name in ("usr", "tmp", "run", "var", "etc", "proc", "sys", "dev", "fixture", "results"):
        (root / name).mkdir()
    for name in ("bin", "sbin", "lib", "lib64"):
        (root / name).symlink_to("usr/" + name)
    (root / "var/run").symlink_to("../run")
    (root / "etc/hosts").write_text("127.0.0.1 localhost\n")
    (root / "etc/nsswitch.conf").write_text("hosts: files\n")
    shutil.copy2(fixture, root / "fixture/bridge-test")
    # Same read-only /usr and private network/mount namespace as 396038636.
    mounts = [dict(destination="/usr", type="bind", source="/usr", options=["rbind", "ro", "nosuid", "nodev"]),
              dict(destination="/results", type="bind", source=str(output), options=["bind", "rw", "nosuid", "nodev"])]
    for destination, kind in (("/proc", "proc"), ("/sys", "sysfs"), ("/tmp", "tmpfs"), ("/run", "tmpfs")):
        mounts.append(dict(destination=destination, type=kind, source=kind))
    caps = ["CAP_SYS_ADMIN", "CAP_NET_ADMIN", "CAP_NET_RAW", "CAP_DAC_OVERRIDE"]
    spec = dict(ociVersion="1.0.2", root=dict(path="rootfs", readonly=True),
                process=dict(args=["/fixture/bridge-test", FILTER, "--gtest_output=xml:/results/test.xml"],
                             cwd="/", user=dict(uid=0, gid=0),
                             env=[*[f"{k}={v}" for k, v in ENV.items()], "TEST_ON_GVISOR=systrap", "GVISOR_NETWORK=none"],
                             capabilities={key: caps for key in ("effective", "permitted", "bounding")}),
                mounts=mounts, linux=dict(namespaces=[dict(type=kind) for kind in ("pid", "network", "ipc", "uts", "mount")]))
    (work / "config.json").write_text(json.dumps(spec))
    # The private tmpfs disappears with the launcher's mount namespace.
    with tempfile.TemporaryDirectory(prefix="bridge-mtu-", dir="/tmp") as state:
        command = [*launcher, "/bin/bash", "-ec", 'mount -t tmpfs -o mode=0700 tmpfs "$1"; shift; exec "$@"',
                   "bridge", state, "/usr/bin/env", "-i", *[f"{k}={v}" for k, v in ENV.items()], str(runtime),
                   "--root=" + state, "--platform=systrap", "--network=none", "--net-raw=true",
                   "--ignore-cgroups=true", "run", "--bundle=" + str(work), "bridge-mtu"]
        try:
            result = subprocess.run(command, env=ENV, timeout=180)
            return result.returncode
        finally:
            if xml.is_file() and xml.stat().st_size:
                shutil.copyfile(xml, os.environ["XML_OUTPUT_FILE"])


if __name__ == "__main__":
    raise SystemExit(main())
