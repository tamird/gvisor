#!/usr/bin/env python3
"""Run the original jtreg case and retain just its first five echo exchanges."""

import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess

WORK = Path(os.environ["JAVA_ECHO"])
IMAGE = "us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/runtimes/java21_x86_64@sha256:c5a1419701eea132bc82193018a09a91296a8d9e96492495a72dd84040e0bb47"
CASE = "java/nio/channels/DatagramChannel/AdaptorBasic.java"
# runsc expands a directory passed to --debug-log to runsc.log.<timestamp>.<command>.txt.
BOOT_LOG = "runsc.log.*.boot.txt"
CALL = re.compile(r" X (recvfrom|sendto)\((0x[0-9a-f]+ [^,]+),.*\) = (-?\d+)(?: errno=(\d+))?")
ENDPOINT = re.compile(r"\{Family: (AF_INET6?), Addr: ([0-9A-Fa-f:.]+), Port: (\d+)\}")


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def address_kind(addr, bridge_addr):
    value = ipaddress.ip_address(addr)
    if isinstance(value, ipaddress.IPv6Address) and value.ipv4_mapped:
        value = value.ipv4_mapped
    return "self" if value == bridge_addr else "other"


def socket_report(logs, client_port, bridge_addr):
    calls = []
    server_socket = None
    replies = 0
    for log in logs:
        # Debug files belong to this job's isolated Docker runtime; they stay on the runner.
        data = subprocess.check_output(["sudo", "-n", "cat", "--", str(log)], text=True)
        for line in data.splitlines():
            match = CALL.search(line)
            if not match:
                continue
            op, fd, result, errno = match.groups()
            endpoint = ENDPOINT.search(line)
            if server_socket is None:
                if not (op == "recvfrom" and result == "42" and endpoint and int(endpoint[3]) == client_port):
                    continue
                server_socket = fd
            if fd != server_socket or (result != "42" and errno != "101"):
                continue
            row = {"call": op, "result": int(result)}
            if errno:
                row["errno"] = int(errno)
            if endpoint:
                row.update(family=endpoint[1], address=address_kind(endpoint[2], bridge_addr), port=int(endpoint[3]))
            calls.append(row)
            if op == "sendto" and result == "42" and endpoint and int(endpoint[3]) == client_port:
                replies += 1
            if replies == 5 or len(calls) == 20 or errno == "101":
                return calls
    return calls


def run(attempt):
    before = set((WORK / "trace").glob(BOOT_LOG))
    jtwork = WORK / f"jtwork-{attempt}"
    jtwork.mkdir()
    args = ["create", "--network=echo54693", "--runtime=echo-ci", "-v", str(jtwork) + ":/jtwork",
            IMAGE, "jtreg", "-agentvm", "-dir:/root/jdk/test/jdk", "-w:/jtwork", "-noreport",
            "-timeoutFactor:5", "-verbose:all", "-tl:200", CASE]
    created = docker(*args)
    assert created.returncode == 0, "Docker could not create the Java container"
    container = created.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{64}", container)
    try:
        started = docker("start", container)
        assert started.returncode == 0, "archived runsc failed to start the Java container"
        info = docker("inspect", "--format", "{{.HostConfig.Runtime}} {{(index .NetworkSettings.Networks \"echo54693\").IPAddress}}", container)
        runtime, ip = info.stdout.split()
        bridge_addr = ipaddress.IPv4Address(ip)
        assert runtime == "echo-ci" and bridge_addr in ipaddress.ip_network("172.30.193.0/24")
        waited = docker("wait", container)
        assert waited.returncode == 0
        status = int(waited.stdout.strip())
    finally:
        docker("rm", "-f", container)
    jtr = (jtwork / CASE.replace(".java", ".jtr")).read_text()
    client = re.search(r"(?m)^src: /\[([0:]+)\]:(\d+)", jtr)
    server = re.search(r"(?m)^dst: /([\d.]+):(\d+)", jtr)
    assert client and server and ipaddress.ip_address(server[1]) == bridge_addr
    first = jtr.split("dst: ", 2)[1]
    logs = sorted(set((WORK / "trace").glob(BOOT_LOG)) - before)
    events = socket_report(logs, int(client[2]), bridge_addr)
    row = {"attempt": attempt, "source": "302d0f545", "case": CASE, "runtime": runtime,
           "containerIPv4": ip, "clientBind": client[1], "clientPort": int(client[2]),
           "serverPort": int(server[2]), "jtregExit": status, "jtregPassed": "execStatus=Passed." in jtr,
           "initialCompleted": len(re.findall(r"(?m)^post op:", first)),
           "serverReportedUnreachable": "Failed to send response: java.net.SocketException: Network is unreachable" in jtr,
           "bootLogCount": len(logs), "serverSocket": events}
    (WORK / "receipt" / f"run-{attempt}.json").write_text(json.dumps(row, indent=2) + "\n")
    print(json.dumps(row), flush=True)
    assert status == 0 and row["jtregPassed"] and row["initialCompleted"] == 5, "jtreg did not complete the original case"
    assert len(events) == 10 and all(e["result"] == 42 and e.get("port") == int(client[2]) for e in events), "trace did not capture five request/reply pairs"
    assert [e["call"] for e in events] == ["recvfrom", "sendto"] * 5


if __name__ == "__main__":
    for iteration in range(1, 4):
        run(iteration)
