"""Read the configured MTU and an IPv4 SYN MSS on one private veth pair."""

import json
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

CLIENT = "192.0.2.1"
SERVER = "192.0.2.2"
PORT = 30149
PEER = "veth-mss-peer"


def emit(phase, **fields):
    print("VETH_WIRE " + json.dumps({"phase": phase, **fields}), flush=True)


def ip(*args, ns=False):
    prefix = ["ip", "-n", PEER] if ns else ["ip"]
    return subprocess.check_output(prefix + list(args), text=True, timeout=8)


def mtu(ifname, ns=False):
    rows = json.loads(ip("-j", "link", "show", "dev", ifname, ns=ns))
    row, = rows
    assert row["ifname"] == ifname
    return row["mtu"]


def syn_options(frame, port):
    # Match only this fixture's client SYN as received on the peer port.
    if len(frame) < 54 or frame[12:14] != b"\x08\x00" or frame[23] != socket.IPPROTO_TCP:
        return None
    if frame[26:34] != socket.inet_aton(CLIENT) + socket.inet_aton(SERVER):
        return None
    tcp = frame[14 + 4 * (frame[14] & 15):]
    if len(tcp) < 20 or struct.unpack_from("!H", tcp, 2)[0] != port or tcp[13] & 0x12 != 2:
        return None
    end = (tcp[12] >> 4) * 4
    assert 20 <= end <= len(tcp)
    i, mss, timestamps = 20, None, False
    while i < end:
        kind = tcp[i]
        if kind == 0:
            break
        if kind == 1:
            i += 1
            continue
        assert i + 1 < end
        n = tcp[i+1]
        assert n >= 2 and i+n <= end
        if kind == 2:
            assert n == 4 and mss is None
            mss = struct.unpack_from("!H", tcp, i+2)[0]
        if kind == 8:
            assert n == 10
            timestamps = True
        i += n
    assert mss is not None
    return mss, timestamps


def server(port):
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800)) as capture, socket.socket() as listener:
        capture.bind(("mvpeer", 0))
        capture.settimeout(5)
        listener.settimeout(6)
        listener.bind((SERVER, port))
        listener.listen(1)
        print("LISTENING", flush=True)
        conn, addr = listener.accept()
        with conn:
            conn.settimeout(5)
            assert addr[0] == CLIENT and conn.recv(1) == b"v"
            conn.sendall(b"w")
        deadline = time.monotonic() + 5
        for _ in range(48):
            capture.settimeout(max(0.01, deadline-time.monotonic()))
            if (opts := syn_options(capture.recv(2048), port)) is not None:
                print(json.dumps({"mss": opts[0], "timestamps": opts[1]}), flush=True)
                return
        raise RuntimeError("missing matching veth SYN")


def observe(configured, port):
    p = subprocess.Popen(["ip", "netns", "exec", PEER, sys.executable, str(Path(__file__).resolve()), "receive", str(port)],
                         text=True, stdout=subprocess.PIPE)
    try:
        assert p.stdout.readline().strip() == "LISTENING", "peer listener did not start"
        with socket.socket() as client:
            client.settimeout(6)
            client.bind((CLIENT, 0))
            client.connect((SERVER, port))
            client.sendall(b"v")
            assert client.recv(1) == b"w"
        result, _ = p.communicate(timeout=8)
        assert p.returncode == 0, "peer failed"
        emit("transfer", configured=configured, local=mtu("mvlocal"), peer=mtu("mvpeer", ns=True),
             port=port, exchange=True, **json.loads(result))
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


def run():
    # This process is already inside a fresh private mount and network namespace.
    initial = mtu("lo")
    try:
        ip("link", "set", "lo", "mtu", "1234")
        observed = mtu("lo")
        emit("loopback", initial=initial, requested=1234, observed=observed)
    finally:
        delta = 1234 - mtu("lo")
        assert delta in (0, 14)
        ip("link", "set", "lo", "mtu", str(initial+delta))
        emit("restored_loopback", observed=mtu("lo"), initial=initial)
        assert mtu("lo") == initial
    ip("netns", "add", PEER)
    try:
        ip("link", "add", "name", "mvlocal", "type", "veth", "peer", "name", "mvpeer", "netns", PEER)
        for dev, ns, addr in (("mvlocal", False, CLIENT), ("mvpeer", True, SERVER)):
            ip("addr", "add", addr+"/30", "dev", dev, ns=ns)
            ip("link", "set", dev, "up", ns=ns)
        observe(1500, PORT)
        ip("link", "set", "mvlocal", "mtu", "3000")
        ip("link", "set", "mvpeer", "mtu", "3000", ns=True)
        observe(3000, PORT+1)
    finally:
        ip("netns", "del", PEER)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "receive":
        server(int(sys.argv[2]))
    else:
        assert len(sys.argv) == 1
        run()
