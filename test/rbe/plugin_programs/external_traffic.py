#!/usr/bin/env python3
"""Exchange unpredictable TCP/UDP data across a container's Docker bridge."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET


IMAGE = "python@sha256:69ebe56e2a764c399514a0a6117131cb730cc4d0e71e85c08deb9af74cbb9f41"
RUNTIME = "runsc-qualified-plugin"
PORT = 18751
REQUEST = 257
RESPONSE = 383
KINDS = {"tcp": socket.SOCK_STREAM, "udp": socket.SOCK_DGRAM}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def response(proto, request):
    return hashlib.shake_256(b"guest/" + proto.encode() + b"/" + request).digest(RESPONSE)


def read_exact(sock, length):
    data = bytearray()
    while len(data) < length:
        part = sock.recv(length - len(data))
        if not part:
            raise EOFError(len(data))
        data.extend(part)
    return bytes(data)


def error(exc):
    row = {"type": type(exc).__name__, "errno": getattr(exc, "errno", None),
           "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    if isinstance(exc, subprocess.CalledProcessError):
        row.update(command=[str(arg) for arg in exc.cmd[:2]], exit=exc.returncode, diagnostic=(exc.stderr or "")[-600:])
    return row


def emit(item):
    print(json.dumps(item), flush=True)


def guest():
    def serve(proto, listener):
        result = {"protocol": proto}
        try:
            if proto == "tcp":
                sock, peer = listener.accept()
                with sock:
                    sock.settimeout(12)
                    data = read_exact(sock, REQUEST)
                    out = response(proto, data)
                    sock.sendall(out)
            else:
                data, peer = listener.recvfrom(2048)
                out = response(proto, data)
                assert listener.sendto(out, peer) == len(out)
            result.update(peer=peer, received=[len(data), digest(data)], sent=[len(out), digest(out)])
        except Exception as exc:
            result["error"] = error(exc)
        emit(result)

    listeners = {}
    for proto, kind in KINDS.items():
        try:
            listener = socket.socket(socket.AF_INET, kind)
            listener.bind(("0.0.0.0", PORT))
            if proto == "tcp":
                listener.listen(1)
            listener.settimeout(20)
            listeners[proto] = listener
        except OSError as exc:
            emit({"bind": proto, "error": error(exc)})
            raise
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = [pool.submit(serve, proto, listener) for proto, listener in listeners.items()]
        emit({"ready": True})
        for task in work:
            task.result()


def docker(*args, timeout=20):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.PIPE, timeout=timeout).strip()


def logs(cid):
    rows = []
    for line in docker("logs", cid).splitlines():
        if line.startswith("{"):
            rows.append(json.loads(line))
    return rows


def capture(sock, stop, guest_ip, gateway, payloads, records):
    # Observe only the private fixture's four exact payloads and endpoint tuples.
    while not stop.is_set():
        try:
            frame = sock.recv(65536)
        except TimeoutError:
            continue
        if len(frame) < 54 or frame[12:14] != b"\x08\x00":
            continue
        ip = frame[14:]
        src, dst = socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
        proto = {6: "tcp", 17: "udp"}.get(ip[9])
        if not proto or {src, dst} != {gateway, guest_ip}:
            continue
        transport = ip[(ip[0] & 15) * 4:struct.unpack_from("!H", ip, 2)[0]]
        sport, dport = struct.unpack_from("!HH", transport)
        if (sport if src == guest_ip else dport) != PORT:
            continue
        data = transport[8 if proto == "udp" else (transport[12] >> 4) * 4:]
        direction = "reply" if src == guest_ip else "request"
        if data == payloads[proto][direction]:
            records.setdefault(proto + "_" + direction, {
                "src": [src, sport], "dst": [dst, dport], "bytes": len(data), "sha256": digest(data),
                "src_mac": frame[6:12].hex(":"), "dst_mac": frame[:6].hex(":")})


def exchange(proto, guest_ip, gateway, payloads):
    row = {}
    try:
        with socket.socket(socket.AF_INET, KINDS[proto]) as client:
            client.bind((gateway, 0))
            client.settimeout(8)
            client.connect((guest_ip, PORT))
            row.update(local=client.getsockname(), peer=client.getpeername())
            if proto == "tcp":
                client.sendall(payloads["request"])
                reply = read_exact(client, RESPONSE)
            else:
                assert client.send(payloads["request"]) == REQUEST
                reply = client.recv(2048)
            row.update(sent=[REQUEST, digest(payloads["request"])], received=[len(reply), digest(reply)])
            assert reply == payloads["reply"]
            row["ok"] = True
    except Exception as exc:
        row["error"] = error(exc)
    return row


def observe(runtime, row):
    payloads = {}
    for proto in KINDS:
        request = secrets.token_bytes(REQUEST)
        payloads[proto] = {"request": request, "reply": response(proto, request)}
    # The guest never sees the host's challenge except through its sockets.
    cid = docker("run", "-d", "--network=bridge", "--runtime=" + runtime,
                 "--entrypoint=/usr/local/bin/python3", IMAGE, "-u", "-c", Path(__file__).read_text(), "guest")
    try:
        info, = json.loads(docker("inspect", cid))
        net = info["NetworkSettings"]["Networks"]["bridge"]
        guest_ip, gateway = net["IPAddress"], net["Gateway"]
        row.update(image=info["Image"], runtime=info["HostConfig"]["Runtime"], guest_ip=guest_ip, gateway=gateway,
                   guest_mac=net["MacAddress"])
        assert row["runtime"] == runtime and not info["HostConfig"]["PortBindings"]
        assert guest_ip != gateway and all(not ipaddress.ip_address(ip).is_loopback for ip in (guest_ip, gateway))
        bridge, = json.loads(docker("network", "inspect", "bridge"))
        iface = bridge["Options"]["com.docker.network.bridge.name"]
        row.update(interface=iface, host_mac=Path("/sys/class/net", iface, "address").read_text().strip())
        assert iface == "docker0"
        until = time.monotonic() + 25
        while {"ready": True} not in logs(cid):
            if time.monotonic() >= until or docker("inspect", "-f", "{{.State.Running}}", cid) != "true":
                raise RuntimeError("guest did not bind both sockets")
            time.sleep(0.1)
        row.update(ready=True, host={}, wire={})
        stop = threading.Event()
        # Outbound taps are delivered to ETH_P_ALL listeners.
        with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as sock:
            sock.bind((iface, 0))
            sock.settimeout(0.2)
            with ThreadPoolExecutor(max_workers=3) as pool:
                observer = pool.submit(capture, sock, stop, guest_ip, gateway, payloads, row["wire"])
                try:
                    jobs = {p: pool.submit(exchange, p, guest_ip, gateway, data) for p, data in payloads.items()}
                    row["host"] = {p: job.result() for p, job in jobs.items()}
                    row["guest_exit"] = docker("wait", cid, timeout=23)
                    until = time.monotonic() + 2
                    while len(row["wire"]) < 4 and time.monotonic() < until:
                        time.sleep(0.05)
                finally:
                    stop.set()
                    observer.result()
        results = [v for v in logs(cid) if "protocol" in v]
        row["guest"] = {v["protocol"]: v for v in results}
        assert len(results) == 2 and row["guest_exit"] == "0"
        for proto in KINDS:
            local, remote = row["host"][proto], row["guest"][proto]
            assert local["ok"] and list(local["local"]) == remote["peer"]
            assert local["sent"] == remote["received"] and local["received"] == remote["sent"]
            req, reply = (row["wire"][proto + "_" + direction] for direction in ("request", "reply"))
            assert (req["src"], req["dst"]) == (list(local["local"]), list(local["peer"]))
            assert (reply["src"], reply["dst"]) == (req["dst"], req["src"])
            assert (req["src_mac"], req["dst_mac"]) == (row["host_mac"], row["guest_mac"])
            assert (reply["src_mac"], reply["dst_mac"]) == (row["guest_mac"], row["host_mac"])
        row["ok"] = True
    finally:
        try:
            row["guest_rows"] = logs(cid)
        finally:
            docker("rm", "-f", cid)


def main():
    if sys.argv[1:] == ["guest"]:
        guest()
        return
    if sys.argv[1:2] != ["observe"]:
        rc = subprocess.run(["sudo", "-n", sys.executable, str(Path(__file__).resolve()),
                             "observe", os.environ["REPORT_DIR"]]).returncode
        suite = ET.Element("testsuite", tests="1", failures=str(int(bool(rc))))
        case = ET.SubElement(suite, "testcase", name="TestExternalTraffic")
        if rc:
            ET.SubElement(case, "failure", message="See individual native and plugin endpoint/bridge observations.")
        ET.ElementTree(suite).write(os.environ["XML_OUTPUT_FILE"], encoding="utf-8")
        raise SystemExit(rc)
    out = Path(sys.argv[2]) / "wire.json"
    result = {}
    for runtime in ("runc", RUNTIME):
        row = result[runtime] = {}
        try:
            observe(runtime, row)
        except Exception as exc:
            row["error"] = error(exc)
        out.write_text(json.dumps(result, indent=2) + "\n")
    assert all(row.get("ok") for row in result.values())
    assert result["runc"]["image"] == result[RUNTIME]["image"]


if __name__ == "__main__":
    main()
