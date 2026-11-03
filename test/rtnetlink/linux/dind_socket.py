"""The #16908 echo exchange; record which operation raises the first exception."""

import collections
import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import sys
import threading
import time


def server(mode):
    def handle(c):
        try:
            c.recv(1)
            c.sendall(b"x")
            if mode == "client-first":
                while c.recv(16):
                    pass
            c.close()
        except Exception:
            c.close()

    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 9000))
    s.listen(512)
    while True:
        c, _ = s.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


def client(mode, n):
    assert n == 2000 and mode in ("client-first", "server-first")
    first = None
    failures = collections.Counter()
    echoes = eof = done = 0
    start = time.monotonic()
    for i in range(n):
        try:
            stage = "create_connection"
            c = socket.create_connection(("srv", 9000), timeout=3)
            stage = "sendall"
            c.sendall(b"a")
            stage = "recv_echo"
            if c.recv(1) != b"x":
                raise ConnectionError("empty reply")
            echoes += 1
            if mode == "server-first":
                stage = "recv_eof"
                while c.recv(16):
                    pass
                eof += 1
            stage = "close"
            c.close()
            done += 1
        except Exception as e:
            key = (stage, type(e).__name__, getattr(e, "errno", None))
            failures[key] += 1
            if first is None:
                first = dict(index=i, stage=stage, error=key[1], errno=key[2],
                             errno_name=errno.errorcode.get(key[2]))
                print("FIRST " + json.dumps(first), flush=True)
    elapsed = time.monotonic() - start
    result = dict(mode=mode, attempted=n, completed=done, echoes=echoes, eofs=eof,
                  elapsed_seconds=elapsed, first=first,
                  errors=[dict(stage=k[0], error=k[1], errno=k[2], count=v) for k,v in failures.items()])
    print("TALLY " + json.dumps(result), flush=True)
    # Inspect name service after the loop, so the first attempted connection is also
    # the client's first resolution of "srv".
    root = Path("/proc/sys/net/ipv4")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        ifreq = fcntl.ioctl(probe, 0x8921, struct.pack("16s24x", b"eth0"))  # SIOCGIFMTU
    result.update(srv_ips=sorted({v[4][0] for v in socket.getaddrinfo("srv",9000,type=socket.SOCK_STREAM)}),
                  dns=[l.split()[1] for l in Path("/etc/resolv.conf").read_text().splitlines() if l.startswith("nameserver ")],
                  static_srv=any("srv" in l.split()[1:] for l in Path("/etc/hosts").read_text().splitlines()),
                  mtu=struct.unpack_from("i", ifreq, 16)[0], python=sys.version.split()[0],
                  sysctls={k:(root/k).read_text().strip() if (root/k).is_file() else None
                           for k in ("tcp_timestamps","tcp_rfc1337","ip_local_port_range")})
    print("CELL " + json.dumps(result), flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        server(os.environ["MODE"])
    else:
        client(sys.argv[1], int(sys.argv[2]))
