#!/usr/bin/env python3
"""Run only the July packaged Sentry with the two pinned Docker image inputs."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from packaged_bridge import ENV, FILES, RELEASES, WORK, diagnose, digest, text

CAPS = ["CAP_" + c for c in """AUDIT_WRITE CHOWN DAC_OVERRIDE FOWNER FSETID KILL MKNOD
NET_ADMIN NET_BIND_SERVICE NET_RAW SETFCAP SETGID SETPCAP SETUID SYS_ADMIN SYS_CHROOT SYS_PTRACE""".split()]
HOST_SETUP = r"""
set -eu
mount -t tmpfs -o mode=0700,size=2G tmpfs "$1"
mkdir -p "$1/bundle/rootfs" "$1/state"
tar -xf "$2" -C "$1/bundle/rootfs"
cp "$3" "$1/bundle/config.json"
exec "$4" --root="$1/state" --platform=systrap --network=none --net-raw=true --ignore-cgroups=true run --bundle="$1/bundle" nesteddocker
"""


def main():
    record = {"source": "9f3e9a3fba9416ea1f5832d82923d95830479096", "release": "20260727.0",
              "guest_caps": CAPS, "steps": [], "cells": [], "first_errors": [], "server_ips": {}, "snat": {}}
    try:
        package = FILES / "july.deb"
        assert digest(package) == RELEASES["july"][1] and text("dpkg-deb", "-f", package, "Version") == record["release"]
        binaries = WORK / "release"
        subprocess.run(["dpkg-deb", "-x", package, binaries], check=True, env=ENV)
        runtime = binaries / "usr/bin/runsc"
        record["version"] = text(runtime, "--version").splitlines()[0]
        assert record["version"].endswith(record["release"])
        record["binaries"] = {str(p.relative_to(binaries)):digest(p) for p in [runtime, *sorted((runtime.parent/"gvisor-bin").glob("*"))] if p.is_file()}
        qa = WORK / "qa"
        qa.mkdir()
        for name in ("dind_socket.py", "dind_guest.sh", "python-image.tar"):
            shutil.copyfile(FILES / name, qa / name)
        mounts = [dict(destination=d, type=t, source=t, options=opts) for d,t,opts in (
            ("/proc","proc",[]), ("/sys","sysfs",[]), ("/sys/fs/cgroup","cgroup",["rw"]),
            ("/tmp","tmpfs",["mode=1777"]), ("/run","tmpfs",["mode=0755"]),
            ("/dev/shm","tmpfs",["mode=1777"]), ("/var/lib/docker","tmpfs",["size=2g","mode=0711"]))]
        mounts.append(dict(destination="/qa",type="bind",source=str(qa),options=["bind","ro","nosuid","nodev","noexec"]))
        spec = dict(ociVersion="1.0.2",root=dict(path="rootfs",readonly=False),hostname="dind",
                    process=dict(args=["/bin/sh","/qa/dind_guest.sh"],cwd="/",user=dict(uid=0,gid=0),
                                 env=["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin","HOME=/root"],
                                 capabilities={k:CAPS for k in ("bounding","effective","permitted")}),
                    mounts=mounts,linux=dict(namespaces=[dict(type=k) for k in ("pid","network","ipc","uts","mount")]))
        config = WORK / "bundle.json"
        config.write_text(json.dumps(spec))
        with tempfile.TemporaryDirectory(prefix="di16908-",dir="/tmp") as state:
            args = ["sudo","-n","/usr/bin/timeout","--kill-after=10","675","unshare","--mount","--net","--propagation","private",
                    "/usr/bin/env","-i","PATH=/usr/sbin:/usr/bin:/sbin:/bin","LC_ALL=C",
                    "/bin/bash","-ec",HOST_SETUP,"dind",state,(FILES/"dind-rootfs.tar").resolve(),config,runtime]
            p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV, timeout=705)
        record["runtime_exit"] = p.returncode
        signals = ("unrecognized","unsupported","not found","invalid","read-only","no such","denied","not permitted",
                   "error","failed","cannot","bad address","no space","wrong fs type","usage:","unrecognized option")
        selected = [l for l in p.stdout.splitlines() if any(s in l.lower() for s in signals)]
        record["diagnostics"] = diagnose("\n".join(selected[:15]+selected[-15:]))
        for line in p.stdout.splitlines():
            if line.startswith("DIND_STEP "):
                record["steps"].append(line.split()[1])
            elif line.startswith("DIND_SNAT "):
                _, proto, status, fw = line.split()
                record["snat"][proto] = [status, fw]
            elif line.startswith("DIND_DAEMON "):
                record["daemon"],record["driver"] = line.split()[1:]
            elif line.startswith("DIND_NETWORK "):
                record["network_id"],record["subnet"] = line.split()[1:]
            elif line.startswith("DIND_SERVER "):
                _, mode, addr = line.split()
                record["server_ips"][mode] = addr
            elif line.startswith("CELL "):
                record["cells"].append(json.loads(line[5:]))
            elif line.startswith("FIRST "):
                record["first_errors"].append(json.loads(line[6:]))
        assert p.returncode == 0 and record["steps"][-1] == "finished" and record["daemon"] == "27.5.1"
        assert len(record["network_id"]) == 64 and set(record["snat"]) == {"tcp","udp"}
        assert [c["mode"] for c in record["cells"]] == ["client-first","server-first"]
        for c in record["cells"]:
            failed = sum(e["count"] for e in c["errors"])
            assert c["attempted"] == c["completed"] + failed == 2000 and (c["first"] is None) == (failed == 0)
            assert c["completed"] <= c["echoes"] <= 2000
            assert c["eofs"] == 0 if c["mode"] == "client-first" else c["completed"] <= c["eofs"] <= c["echoes"]
            assert c["dns"] == ["127.0.0.11"] and not c["static_srv"]
            assert c["srv_ips"] == [record["server_ips"][c["mode"]]] and c["python"].startswith("3.12.")
        record["complete"] = True
    except Exception as exc:
        record["error_type"] = type(exc).__name__
        record["complete"] = False
    finally:
        print("NESTED16908 " + json.dumps(record), flush=True)
    return not record["complete"]


if __name__ == "__main__":
    raise SystemExit(main())
