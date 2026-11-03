#!/usr/bin/env python3
"""Read both veth MTUs after each netlink write in a new Linux namespace."""

import json
import os
from pathlib import Path
import platform
import subprocess
import sys


PORTS = ("mtu-test-a", "mtu-test-b")


def snapshot(label):
    result = []
    for name in PORTS:
        rows = json.loads(subprocess.check_output(["ip", "-j", "-d", "link", "show", "dev", name]))
        row, = rows
        assert row["ifname"] == name and row["linkinfo"]["info_kind"] == "veth"
        result.append(row)
    for endpoint, peer in (result, reversed(result)):
        assert endpoint.get("link") == peer["ifname"] or endpoint.get("link_index") == peer["ifindex"]
    mtus = [row["mtu"] for row in result]
    print("VETH_MTU " + json.dumps({"step": label, "mtus": mtus}), flush=True)
    return mtus


def main():
    if len(sys.argv) == 1:
        subprocess.run([
            "sudo", "-n", "unshare", "--net", "--mount", "--propagation", "private",
            "/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL=C",
            "/usr/bin/python3", str(Path(__file__).resolve()), os.readlink("/proc/self/ns/net")
        ], check=True)
        return
    assert os.readlink("/proc/self/ns/net") != sys.argv[1]
    assert [r["ifname"] for r in json.loads(subprocess.check_output(["ip", "-j", "link", "show"]))] == ["lo"]
    print("VETH_HOST " + json.dumps({"kernel": platform.release(), "arch": platform.machine(),
                                      "iproute2": subprocess.check_output(["ip", "-V"], text=True).strip()}), flush=True)
    subprocess.run(["ip", "link", "add", PORTS[0], "type", "veth", "peer", "name", PORTS[1]], check=True)
    try:
        assert snapshot("created") == [1500, 1500]
        subprocess.run(["ip", "link", "set", "dev", PORTS[0], "mtu", "3000"], check=True)
        assert snapshot("set-a-3000") == [3000, 1500]
        subprocess.run(["ip", "link", "set", "dev", PORTS[1], "mtu", "1460"], check=True)
        assert snapshot("set-b-1460") == [3000, 1460]
    finally:
        subprocess.run(["ip", "link", "del", "dev", PORTS[0]], check=True)


if __name__ == "__main__":
    main()
