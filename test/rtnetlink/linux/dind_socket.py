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


FLOW = Path("/flow")


class EchoHeaders:
    """Observe just the fixed echo service on this nested container's eth0."""

    def __init__(self, server_ip):
        self.server = socket.inet_aton(server_ip)
        self.rows = collections.defaultdict(list)
        self.more = collections.Counter()
        self.lock = threading.Lock()
        self.error = None
        # ETH_P_ALL is required for outgoing headers. SOCK_DGRAM omits Ethernet.
        try:
            self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(3))
            self.sock.bind(("eth0", 3))
            self.sock.settimeout(0.1)
        except OSError as exc:
            self.error = exc.errno
            return
        threading.Thread(target=self.observe, daemon=True).start()

    def observe(self):
        try:
            while True:
                try:
                    h, addr = self.sock.recvfrom(40)  # IPv4 + fixed TCP header, no payload.
                except TimeoutError:
                    continue
                if len(h) != 40 or addr[1] != 0x0800 or h[0] != 0x45 or h[9] != 6:
                    continue
                if int.from_bytes(h[6:8], "big") & 0x3fff:
                    continue
                sp, dp, seq, ack = struct.unpack_from("!HHII", h, 20)
                if dp == 9000 and h[16:20] == self.server:
                    peer, port, direction = h[12:16], sp, "C"
                elif sp == 9000 and h[12:16] == self.server:
                    peer, port, direction = h[16:20], dp, "S"
                else:
                    continue
                size = int.from_bytes(h[2:4], "big") - 20 - 4 * (h[32] >> 4)
                key = (socket.inet_ntoa(peer), port)
                with self.lock:
                    rows = self.rows[key]
                    if len(rows) < 80:
                        rows.append([direction, addr[2], h[33], seq, ack, size, time.monotonic_ns()])
                    else:
                        self.more[key] += 1
        except OSError as exc:
            self.error = exc.errno

    def report(self, samples):
        with self.lock:
            return {name: dict(headers=list(self.rows[tuple(key)]),
                               truncated=self.more[tuple(key)], observer_errno=self.error)
                    for name, key in samples.items() if key}


def share(name, value):
    tmp = FLOW / (name + ".tmp")
    tmp.write_text(json.dumps(value))
    tmp.rename(FLOW / name)


def server(mode):
    # Docker provides the local container address in its own /etc/hosts.
    trace = EchoHeaders(socket.gethostbyname(socket.gethostname()))
    accepts = collections.defaultdict(list)

    def finish_report():
        while not (FLOW / "request").exists():
            time.sleep(0.05)
        samples = json.loads((FLOW / "request").read_text())
        time.sleep(0.2)  # The client finished this close order before requesting the report.
        rows = trace.report(samples)
        for name, sample in samples.items():
            if sample:
                rows[name]["accepted"] = accepts[tuple(sample)][:8]
        share("response", rows)

    threading.Thread(target=finish_report, daemon=True).start()

    def handle(c, state):
        try:
            state["received"] = len(c.recv(1))
            c.sendall(b"x")
            state["sent"] = True
            if mode == "client-first":
                while c.recv(16):
                    pass
            c.close()
        except Exception as exc:
            state["errno"] = getattr(exc, "errno", None)
            c.close()

    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 9000))
    s.listen(512)
    while True:
        c, peer = s.accept()
        state = dict(received=None, sent=False, errno=None)
        accepts[peer].append(state)
        threading.Thread(target=handle, args=(c, state), daemon=True).start()


def client(mode, n):
    assert n == 2000 and mode in ("client-first", "server-first")
    first = None
    trace = EchoHeaders(os.environ["ECHO_IP"])
    samples = {}
    ordinals = collections.defaultdict(list)
    local = None
    # create_connection closes its socket on error. Observe its bound address
    # immediately before that close, without replacing the exception or retrying.
    connect = socket.socket.connect

    def observe_connect(s, address):
        nonlocal local
        try:
            return connect(s, address)
        finally:
            try:
                host, port = s.getsockname()[:2]
                local = (host, port) if port and host != "0.0.0.0" else None
            except OSError:
                local = None

    socket.socket.connect = observe_connect
    failures = collections.Counter()
    echoes = eof = done = 0
    start = time.monotonic()
    for i in range(n):
        local = None
        begin = time.monotonic_ns()
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
            samples.setdefault("success", local)
        except Exception as e:
            key = (stage, type(e).__name__, getattr(e, "errno", None))
            failures[key] += 1
            if first is None:
                samples["first"] = local
                first = dict(index=i, stage=stage, error=key[1], errno=key[2],
                             errno_name=errno.errorcode.get(key[2]))
                print("FIRST " + json.dumps(first), flush=True)
            if key[2] == errno.ECONNRESET and local and not samples["first"]:
                samples.setdefault("first_bound_reset", local)
        if local:
            ordinals[local].append([i, begin, time.monotonic_ns(), stage])
    elapsed = time.monotonic() - start
    result = dict(mode=mode, attempted=n, completed=done, echoes=echoes, eofs=eof,
                  elapsed_seconds=elapsed, first=first,
                  errors=[dict(stage=k[0], error=k[1], errno=k[2], count=v) for k,v in failures.items()])
    print("TALLY " + json.dumps(result), flush=True)
    socket.socket.connect = connect
    share("request", samples)
    deadline = time.monotonic() + 4
    while not (FLOW / "response").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    result["flow"] = dict(client=trace.report(samples), endpoints=samples,
                          indices={k: ordinals[v][:16] for k, v in samples.items() if v},
                          server=json.loads((FLOW / "response").read_text())
                          if (FLOW / "response").exists() else None)
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
