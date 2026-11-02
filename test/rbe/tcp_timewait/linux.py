"""Measure a real Linux TIME_WAIT collision in an isolated loopback netns."""
import errno
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def sample(timestamps):
    assert os.readlink("/proc/self/ns/net") != sys.argv[2]
    root = Path("/proc/sys/net/ipv4")
    subprocess.run(["/usr/sbin/ip", "link", "set", "lo", "up"], check=True)
    for name, value in {"ip_local_port_range": "40000 40000", "tcp_timestamps": str(timestamps),
                        "tcp_tw_reuse": "2", "tcp_tw_reuse_delay": "1000"}.items():
        p = root / name
        p.write_text(value)
        assert p.read_text().split() == value.split(), name
    addr = ("127.0.0.1", 35001)
    with socket.socket() as listener:
        listener.bind(addr)
        listener.listen()
        with socket.create_connection(addr, timeout=3) as client, listener.accept()[0] as peer:
            assert client.getsockname()[1] == 40000
            bits = client.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 128)[5]
            assert (bits & 1) == timestamps, bits
            client.shutdown(socket.SHUT_WR)
            assert peer.recv(1) == b""
            peer.shutdown(socket.SHUT_WR)
            assert client.recv(1) == b""

        def in_time_wait():
            for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
                fields = line.split()
                if fields[1:4] == ["0100007F:9C40", "0100007F:88B9", "06"]:
                    return True
            return False

        assert in_time_wait()
        start = time.monotonic()
        results = []
        for after in (0, 1.25):
            time.sleep(max(0, after - (time.monotonic() - start)))
            assert in_time_wait()
            with socket.socket() as second:
                second.settimeout(2)
                elapsed = round((time.monotonic() - start) * 1000)
                rc = second.connect_ex(addr)
                if rc == 0:
                    with listener.accept()[0] as replacement:
                        assert second.getsockname()[1] == replacement.getpeername()[1] == 40000
                        second.sendall(b"B")
                        assert replacement.recv(1) == b"B"
                results.append([elapsed, rc])
        print("TIMEWAIT_LINUX " + json.dumps({"timestamps": timestamps, "info": bits, "port": 40000,
                                               "results": results, "kernel": os.uname().release}), flush=True)
        assert results[0][0] < 1000 and results[0][1] == errno.EADDRNOTAVAIL
        assert results[1][0] >= 1000 and results[1][1] == (0 if timestamps else errno.EADDRNOTAVAIL)


if len(sys.argv) == 3:
    sample(int(sys.argv[1]))
else:
    for ts in (0, 1):
        subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/unshare", "--net", "--",
                        sys.executable, str(Path(__file__).resolve()), str(ts), os.readlink("/proc/self/ns/net")], check=True)
