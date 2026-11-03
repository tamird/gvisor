"""Ordinary short TCP exchanges in the two namespaces of bridge_test.sh."""

import collections
import errno
import ipaddress
import json
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

COUNT = 512
PORT = 9000
SCRIPT = str(Path(__file__).resolve())
IP = {"bridge": ("192.168.0.1", "192.168.0.2"), "loopback": ("127.0.0.1", "127.0.0.1")}


def issue(exc):
    return type(exc).__name__, errno.errorcode.get(exc.errno, str(exc.errno))


def server(link, closer):
    _, dst = IP[link]
    counts = collections.Counter()
    stop = threading.Event()
    lock = threading.Lock()

    def count(key):
        with lock:
            counts[key] += 1

    def exchange(peer):
        stage = "receive"
        try:
            with peer:
                peer.settimeout(3)
                if peer.recv(1) != b"a":
                    count("wrong request or early EOF")
                    return
                count("received")
                stage = "send"
                peer.sendall(b"x")
                count("echoed")
                if closer == "client":
                    stage = "wait EOF"
                    if peer.recv(1) != b"":
                        count("unexpected trailing byte")
                        return
                    count("client EOF")
                stage = "close"
            count("closed")
        except OSError as exc:
            count("/".join((stage, *issue(exc))))

    threads = []
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((dst, PORT))
        listener.listen(512)
        listener.settimeout(.1)

        def accept():
            while not stop.is_set():
                try:
                    peer, _ = listener.accept()
                except TimeoutError:
                    continue
                count("accepted")
                thread = threading.Thread(target=exchange, args=(peer,))
                thread.start()
                threads.append(thread)

        acceptor = threading.Thread(target=accept)
        acceptor.start()
        print("READY", flush=True)
        sys.stdin.readline()
        stop.set()
        acceptor.join()
    for thread in threads:
        thread.join()
    print(json.dumps(counts), flush=True)


def tap(iface):
    sock = None
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        sock.bind((iface, 0))
        sock.setblocking(False)
        return sock, None
    except OSError as exc:
        if sock:
            sock.close()
        return None, issue(exc)


def controls(sensor, link):
    """Drain only SYN/FIN/RST metadata on the fixture's one server port."""
    packets = []
    if sensor is None:
        return packets
    src, dst = IP[link]
    while True:
        try:
            pkt = sensor.recv(128)
        except BlockingIOError:
            return packets
        offset = 14 if pkt[12:14] == b"\x08\x00" else 0
        ip = pkt[offset:]
        if len(ip) < 40 or ip[0] >> 4 != 4 or ip[9] != 6:
            continue
        a, b = (str(ipaddress.IPv4Address(ip[i:i + 4])) for i in (12, 16))
        tcp = ip[(ip[0] & 15) * 4:]
        if len(tcp) < 20 or not (tcp[13] & 7):
            continue
        sport, dport, seq, ack = struct.unpack_from("!HHII", tcp)
        if (a, b, dport) == (src, dst, PORT):
            row = ("client", sport, tcp[13], seq, ack)
        elif (a, b, sport) == (dst, src, PORT):
            row = ("server", dport, tcp[13], seq, ack)
        else:
            continue
        if len(packets) < 128 and (not packets or packets[-1] != row):
            packets.append(row)


def observation(events, sock_port):
    syns = {(port, seq) for side, port, flags, seq, _ in events
            if side == "client" and flags & 2 and not flags & 16}
    if len(syns) != 1:
        return {"binding": "no unique client SYN", "socket_port": sock_port}
    port, iss = syns.pop()
    if sock_port and sock_port != port:
        return {"binding": "socket/SYN mismatch", "socket_port": sock_port}
    wire = [r for r in events if r[1] == port]
    peer = {seq for side, _, flags, seq, ack in wire if side == "server" and flags & 18 == 18
            and ack == (iss + 1) % 2**32}
    rst = next((r for r in wire if r[2] & 4), None)
    bound = False
    if rst:
        side, _, flags, seq, ack = rst
        other = {iss} if side == "server" else peer
        own = peer if side == "server" else {iss}
        # One byte and a FIN can advance both sides by at most 3 from the ISN.
        bound = ((flags & 16 and any(1 <= (ack - s) % 2**32 <= 3 for s in other))
                 or any(1 <= (seq - s) % 2**32 <= 3 for s in own))
    return {"binding": "socket+SYN" if sock_port else "single outstanding SYN",
            "port": port, "synack": len(peer) == 1,
            "first_fin": next((r[0] for r in wire if r[2] & 1), None),
            "first_rst": rst[0] if bound else None,
            "unmatched_rst": bool(rst and not bound),
            "headers": [[side, f, seq, ack] for side, _, f, seq, ack in wire][:16]}


def client(link, closer):
    src, dst = IP[link]
    iface = "veth0" if link == "bridge" else "lo"
    sensor, sensor_error = tap(iface)
    counts = collections.Counter()
    outcomes = collections.Counter()
    wire_counts = collections.Counter()
    examples = {}
    first_rst_attempt = None
    started = time.monotonic()
    done = 0
    for i in range(COUNT):
        if time.monotonic() - started > 50:
            break
        begin = time.monotonic()
        # No outstanding application connection at this point. Late packets from
        # an earlier attempt must not be assigned to this one.
        controls(sensor, link)
        s = socket.socket()
        stage = "connect"
        port = 0
        outcome = "ok"
        try:
            s.settimeout(3)
            s.connect((dst, PORT))
            counts["connected"] += 1
            address, port = s.getsockname()
            if address != src:
                raise AssertionError("unexpected client address")
            stage = "send"
            s.sendall(b"a")
            counts["sent"] += 1
            stage = "receive"
            if s.recv(1) != b"x":
                outcome = "receive/wrong echo or early EOF"
            elif closer == "server":
                counts["received"] += 1
                stage = "wait EOF"
                if s.recv(1) != b"":
                    outcome = "wait EOF/unexpected trailing byte"
                else:
                    counts["server EOF"] += 1
            else:
                counts["received"] += 1
            stage = "close"
            s.close()
        except OSError as exc:
            outcome = "/".join((stage, *issue(exc)))
            try:
                port = s.getsockname()[1]
            except OSError:
                pass
        finally:
            s.close()
        outcomes[outcome] += 1
        done += 1
        time.sleep(max(0, begin + .05 - time.monotonic()))
        obs = observation(controls(sensor, link), port)
        wire_counts[obs["binding"]] += 1
        if obs.get("first_fin"):
            wire_counts["first FIN/" + obs["first_fin"]] += 1
        if obs.get("first_rst"):
            wire_counts["matched RST/" + obs["first_rst"]] += 1
        if outcome not in examples or (obs.get("first_rst") and first_rst_attempt is None):
            sample = {"attempt": i + 1, "src": src, "dst": dst, "dport": PORT,
                      "client_result": outcome, **obs}
            examples.setdefault(outcome, sample)
            if obs.get("first_rst") and first_rst_attempt is None:
                first_rst_attempt = sample
    packet_stats = None
    if sensor:
        try:
            packet_stats = struct.unpack("=II", sensor.getsockopt(263, 6, 8))
        except OSError:
            pass
        sensor.close()
    config = {}
    for name in ("tcp_timestamps", "tcp_rfc1337", "ip_local_port_range"):
        path = Path("/proc/sys/net/ipv4") / name
        try:
            config[name] = path.read_text().strip()
        except OSError:
            config[name] = None
    print(json.dumps({"attempts": done, "elapsed": time.monotonic() - started, "counts": counts,
                      "outcomes": outcomes, "wire": wire_counts, "examples": examples, "first_rst_attempt": first_rst_attempt, "packet_stats": packet_stats,
                      "iface": iface, "sensor_error": sensor_error, "sysctls": config}), flush=True)


def run(link, closer):
    prefix = ["ip", "netns", "exec"]
    cmd = ["/usr/bin/python3", SCRIPT]
    cl = "test0" if link == "bridge" else "test1"
    with subprocess.Popen([*prefix, "test1", *cmd, "server", link, closer],
                          text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as srv:
        try:
            if not select.select([srv.stdout], [], [], 5)[0] or srv.stdout.readline().strip() != "READY":
                raise RuntimeError("listener did not start")
            raw = subprocess.check_output([*prefix, cl, *cmd, "client", link, closer], timeout=60)
            srv.stdin.write("done\n")
            srv.stdin.flush()
            server_result = srv.communicate(timeout=8)[0]
            if srv.returncode:
                raise RuntimeError("server exit")
            row = {"link": link, "active_closer": closer, "client": json.loads(raw), "server": json.loads(server_result)}
            print("TCP16908 " + json.dumps(row, separators=(",", ":")), flush=True)
        finally:
            if srv.poll() is None:
                srv.kill()


if __name__ == "__main__":
    role, link, closer = sys.argv[1:]
    assert link in IP and closer in {"client", "server"}
    {"run": run, "client": client, "server": server}[role](link, closer)
