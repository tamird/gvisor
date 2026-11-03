#!/usr/bin/env python3
"""Compare the pinned nested-Docker echo workload on the native Linux kernel."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from packaged_bridge import ENV, FILES, WORK, diagnose, digest, text

CAPS = ["CAP_" + c for c in """AUDIT_WRITE CHOWN DAC_OVERRIDE FOWNER FSETID KILL MKNOD
NET_ADMIN NET_BIND_SERVICE NET_RAW SETFCAP SETGID SETPCAP SETUID SYS_ADMIN SYS_CHROOT SYS_PTRACE""".split()]
HOST_SETUP = r"""
set -eu
mount -t tmpfs -o mode=0700,size=2G tmpfs "$1"
mkdir -p "$1/bundle/rootfs" "$1/state"
tar -xf "$2" -C "$1/bundle/rootfs"
cp "$3" "$1/bundle/config.json"
test "$(readlink /proc/self/ns/net)" != "$5"
test "$(readlink /proc/self/ns/mnt)" != "$6"
exec "$4" --root="$1/state" run --bundle="$1/bundle" nesteddocker
"""


def main():
    record = {"fixture_parent": "b40e0eb54f6c36853b4a3ced7d3b663c92ba89ab",
              "runtime": "runc", "kernel": os.uname().release,
              "guest_caps": CAPS, "steps": [], "mtu_changes": [], "tallies": [], "cells": [], "first_errors": [], "server_ips": {}, "snat": {}}
    try:
        runtime = Path(shutil.which("runc", path=ENV["PATH"])).resolve(strict=True)
        record["version"] = text(runtime, "--version").splitlines()[0]
        assert record["version"].startswith("runc version ")
        record["runtime_sha256"] = digest(runtime)
        record["cgroup_version"] = 2 if Path("/sys/fs/cgroup/cgroup.controllers").exists() else 1
        assert record["cgroup_version"] == 2
        qa = WORK / "qa"
        qa.mkdir()
        for name in ("dind_socket.py", "dind_guest.sh", "python-image.tar"):
            shutil.copyfile(FILES / name, qa / name)
        # Use the same Linux iproute2 as the prior receipt. Alpine BusyBox uses
        # SIOCSIFMTU (ENOTTY on this Sentry); iproute2 uses IFLA_MTU instead.
        iptool = WORK / "iproute2"
        iptool.mkdir()
        ip = Path(shutil.which("ip", path=ENV["PATH"])).resolve(strict=True)
        assert ip.is_relative_to("/usr") and ip.is_file()
        libs = [Path(w) for w in text("ldd",ip).split() if w.startswith("/")]
        assert libs and any(p.name == "ld-linux-x86-64.so.2" for p in libs)
        for lib in [ip, *libs]:
            resolved = lib.resolve(strict=True)
            assert resolved.is_relative_to("/usr") and resolved.is_file()
            shutil.copy2(resolved, iptool/lib.name)
        record["iproute2"] = dict(version=text("dpkg-query","-W","-f=${Version}","iproute2"),
                                  sha256=digest(iptool/"ip"))
        mounts = [dict(destination=d, type=t, source=t, options=opts) for d,t,opts in (
            ("/proc","proc",[]), ("/sys","sysfs",[]), ("/sys/fs/cgroup","cgroup",["rw","nosuid","nodev","noexec"]),
            ("/dev","tmpfs",["mode=0755","nosuid"]), ("/dev/pts","devpts",["newinstance","ptmxmode=0666","mode=0620","gid=5"]),
            ("/dev/mqueue","mqueue",["nodev","nosuid","noexec"]),
            ("/tmp","tmpfs",["mode=1777"]), ("/run","tmpfs",["mode=0755"]),
            ("/dev/shm","tmpfs",["mode=1777"]), ("/var/lib/docker","tmpfs",["size=2g","mode=0711"]))]
        mounts.append(dict(destination="/qa",type="bind",source=str(qa),options=["bind","ro","nosuid","nodev","noexec"]))
        mounts.append(dict(destination="/iproute2",type="bind",source=str(iptool),options=["bind","ro","nosuid","nodev"]))
        spec = dict(ociVersion="1.0.2",root=dict(path="rootfs",readonly=False),hostname="dind",
                    process=dict(args=["/bin/sh","/qa/dind_guest.sh"],cwd="/",user=dict(uid=0,gid=0),
                                 env=["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin","HOME=/root"],
                                 capabilities={k:CAPS for k in ("bounding","effective","permitted")}),
                    mounts=mounts,linux=dict(
                        cgroupsPath="/dind16908-" + str(os.getpid()),
                        readonlyPaths=["/proc/sys/kernel", "/proc/sys/fs", "/proc/sys/vm", "/proc/sysrq-trigger"],
                        maskedPaths=["/proc/kcore", "/proc/keys", "/sys/firmware"],
                        namespaces=[dict(type=k) for k in ("pid","network","ipc","uts","mount","cgroup")]))
        config = WORK / "bundle.json"
        config.write_text(json.dumps(spec))
        with tempfile.TemporaryDirectory(prefix="di16908-",dir="/tmp") as state:
            args = ["sudo","-n","/usr/bin/timeout","--kill-after=10","675","unshare","--mount","--net","--propagation","private",
                    "/usr/bin/env","-i","PATH=/usr/sbin:/usr/bin:/sbin:/bin","LC_ALL=C",
                    "/bin/bash","-ec",HOST_SETUP,"dind",state,(FILES/"dind-rootfs.tar").resolve(),config,runtime,os.readlink("/proc/self/ns/net"),os.readlink("/proc/self/ns/mnt")]
            p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV, timeout=705)
        record["runtime_exit"] = p.returncode
        signals = ("unrecognized","unsupported","not found","invalid","read-only","no such","denied","not permitted",
                   "error","failed","cannot","bad address","no space","wrong fs type","usage:","unrecognized option")
        selected = [l for l in p.stdout.splitlines() if any(s in l.lower() for s in signals)]
        record["diagnostics"] = diagnose("\n".join(selected[:15]+selected[-15:]))
        for line in p.stdout.splitlines():
            if line.startswith("DIND_STEP "):
                record["steps"].append(line.split()[1])
            elif line.startswith("DIND_MTU "):
                record["mtu_changes"].append(line.split()[1:])
            elif line.startswith("DIND_ROUTE "):
                record["default_route"] = line[11:][:180]
            elif line.startswith("DIND_FORWARD "):
                record["forward"] = line[13:]
            elif line.startswith("DIND_END "):
                record["guest_end"] = line.split()[1:]
            elif line.startswith("TALLY "):
                record["tallies"].append(json.loads(line[6:]))
            elif line.startswith("DIND_SNAT "):
                _, proto, status, fw = line.split()
                record["snat"][proto] = [status, fw]
            elif line.startswith("DIND_SNAT_COUNT "):
                _, point, proto, count = line.split()
                record.setdefault("snat_packets", {}).setdefault(point, {})[proto] = int(count)
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
        assert record["guest_end"] == ["finished","0"] and record["mtu_changes"][-1][1] == "1460"
        assert record["forward"] == "1" and "dev eth0" in record["default_route"]
        assert len(record["network_id"]) == 64 and set(record["snat"]) == {"tcp","udp"}
        assert record["snat"] == {p: ["configured","iptables-legacy"] for p in ("tcp","udp")}
        points = ("before", "client-first", "server-first")
        assert set(record["snat_packets"]) == set(points)
        for proto in ("tcp","udp"):
            counts = [record["snat_packets"][p][proto] for p in points]
            assert counts == sorted(counts)
        assert record["driver"] == "overlay2" and record["subnet"] == "172.18.0.0/16"
        assert [c["mode"] for c in record["cells"]] == ["client-first","server-first"]
        assert len(record["tallies"]) == len(record["cells"])
        for tally, c in zip(record["tallies"], record["cells"]):
            assert all(c[k] == v for k,v in tally.items())
            failed = sum(e["count"] for e in c["errors"])
            assert c["attempted"] == c["completed"] + failed == 2000 and (c["first"] is None) == (failed == 0)
            assert c["completed"] <= c["echoes"] <= 2000
            assert c["eofs"] == 0 if c["mode"] == "client-first" else c["completed"] <= c["eofs"] <= c["echoes"]
            assert c["dns"] == ["127.0.0.11"] and not c["static_srv"]
            assert c["srv_ips"] == [record["server_ips"][c["mode"]]] and c["python"] == "3.12.16"
            assert c["mtu"] == 1486 and c["sysctls"]["tcp_timestamps"] == c["sysctls"]["tcp_rfc1337"] == "1"
            assert c["sysctls"]["ip_local_port_range"].split() == ["16000", "65535"]
            assert c["srv_ips"] == ["172.18.0.2"]
        record["complete"] = True
    except Exception as exc:
        record["error_type"] = type(exc).__name__
        record["complete"] = False
    finally:
        print("NESTED16908 " + json.dumps(record), flush=True)
    return not record["complete"]


if __name__ == "__main__":
    raise SystemExit(main())
