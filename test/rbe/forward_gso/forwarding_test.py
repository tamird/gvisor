"""Compare a routed TCP transfer with and without host segmentation metadata."""

import collections
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET


SOURCE = "192.0.2.1"
SINK = "198.51.100.2"
PORT = 14011
BLOCK = bytes(range(256)) * 256
LENGTH = 16 * len(BLOCK)
MTU = 1466
SCRIPT = Path(__file__).resolve()


def run(*args):
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(f"{args}: {completed.returncode}: {completed.stderr}")
    return completed.stdout


def serve():
    with socket.socket() as server:
        server.bind((SOURCE, PORT))
        server.listen(1)
        print("ready", flush=True)
        conn, _ = server.accept()
        with conn:
            conn.settimeout(12)
            for _ in range(16):
                conn.sendall(BLOCK)


def receive():
    start = time.monotonic()
    count = 0
    digest = hashlib.sha256()
    with socket.create_connection((SOURCE, PORT), timeout=4) as conn:
        while count < LENGTH and (left := 8 - (time.monotonic() - start)) > 0:
            conn.settimeout(left)
            try:
                chunk = conn.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            count += len(chunk)
            digest.update(chunk)
    print(json.dumps(dict(size=count, seconds=time.monotonic() - start, sha256=digest.hexdigest())))


def capture(output, stop):
    observations = collections.Counter()
    marked = set()
    seen = {}
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800)) as packet:
        packet.setsockopt(263, 15, 1)  # SOL_PACKET, PACKET_VNET_HDR
        packet.bind(("rin", 0))
        packet.settimeout(0.2)
        print("ready", flush=True)
        while not stop.exists():
            try:
                frame = packet.recv(131072)
            except TimeoutError:
                continue
            # Linux prepends a ten-byte virtio_net_hdr. Filter to this transfer.
            ip = frame[24:]
            if len(ip) < 40 or ip[9] != 6 or ip[12:16] != socket.inet_aton(SOURCE) or ip[16:20] != socket.inet_aton(SINK):
                continue
            offset = 4 * (ip[0] & 15)
            if int.from_bytes(ip[offset:offset + 2], "big") != PORT:
                continue
            tcp = ip[offset:]
            ident = (int.from_bytes(ip[4:6], "big"), int.from_bytes(tcp[4:8], "big"), len(tcp)-4*(tcp[12] >> 4))
            seen[ident] = max(seen.get(ident, 0), len(ip))
            observations["largestIP"] = max(observations["largestIP"], len(ip))
            observations["packets"] += 1
            if len(ip) > MTU:
                observations["over_mtu"] += 1
                observations["largest"] = max(observations["largest"], len(ip))
                gso_size = struct.unpack_from("=H", frame, 4)[0]
                if frame[1] & 0x7f == 1 and gso_size:
                    observations["tcpv4_gso"] += 1
                    if ip[6] & 0x40:
                        observations["df_tcpv4_gso"] += 1
                        tcp = ip[offset:]
                        identity = [int.from_bytes(ip[4:6], "big"), int.from_bytes(tcp[4:8], "big"),
                                    len(tcp) - 4*(tcp[12] >> 4)]
                        if len(marked) < 256:
                            marked.add(tuple(identity))
                    observations["max_gso_size"] = max(observations["max_gso_size"], gso_size)
    output.write_text(json.dumps(dict(observations, dfGsoPackets=sorted(marked), packetLengths=[[*k, v] for k,v in seen.items()])))


def unmount_null_netns(path):
    # runsc shares a pinned null gofer namespace below --root even after the
    # container is deleted. Detach it before removing this lab's runtime root.
    while path.exists() and subprocess.run(["mountpoint", "-q", path]).returncode == 0:
        run("umount", "-l", path)


def guest_link(work, host):
    tcp = re.compile(r"(recv|send) tcp 192\.0\.2\.1:14011 -> 198\.51\.100\.2:\d+ len:(\d+) id:0x([0-9a-f]+).*? seqnum:(\d+)")
    target = {tuple(x) for x in host.get("dfGsoPackets", [])}
    lengths = {tuple(x[:3]):x[3] for x in host.get("packetLengths", [])}
    stats = collections.defaultdict(collections.Counter)
    matched = collections.defaultdict(list)
    icmp = 0
    for path in work.glob("debug-*.log"):
        with path.open(errors="replace") as f:
            for line in f:
                if m := tcp.search(line):
                    direction, n, ident, seq = m.groups()
                    n = int(n)
                    key = (int(ident, 16), int(seq), n)
                    row = stats[direction]
                    row["packets"] += 1
                    row["maxPayload"] = max(row["maxPayload"], n)
                    if n > MTU:
                        row["payloadAloneOverMTU"] += 1
                    if key in lengths:
                        row["matchedHostPackets"] += 1
                        row["maxMatchedIPLength"] = max(row["maxMatchedIPLength"], lengths[key])
                    if key in target:
                        row["matchedHostDFGSO"] += 1
                        if " gso:" in line:
                            row["matchedWithGuestGSOMetadata"] += 1
                        if len(matched[direction]) < 3:
                            matched[direction].append(key)
                elif re.search(r"send icmp [0-9.]+ -> 192\.0\.2\.1 destination unreachable .*code:4", line):
                    icmp += 1
    return dict(tcp=dict(stats), matchedExamples=dict(matched), fragNeededSent=icmp)


def spawn(scope, args, stderr=None):
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=stderr, text=True)
    def stop():
        if process.poll() is None:
            process.kill()
        process.wait()
    scope.callback(stop)
    return process


def ready(process):
    if not select.select([process.stdout], [], [], 25)[0]:
        raise TimeoutError(f"startup timed out: {process.args}")
    if "ready" not in (line := process.stdout.readline()):
        raise RuntimeError(f"startup failed: {line!r} ({process.args})")


def compare(mode, root, runtime, helper):
    work = root / mode
    work.mkdir()
    names = ["gso-source", "gso-router", "gso-sink"]
    src, router, sink = names
    with contextlib.ExitStack() as scope:
        for ns in names:
            run("ip", "netns", "add", ns)
            scope.callback(subprocess.run, ["ip", "netns", "del", ns], check=False)
            run("ip", "-n", ns, "link", "set", "lo", "up")
        for n1, dev1, addr1, n2, dev2, addr2 in [
            (src, "left", SOURCE, router, "rin", "192.0.2.2"),
            (router, "rout", "198.51.100.1", sink, "right", SINK),
        ]:
            run("ip", "link", "add", dev1, "netns", n1, "type", "veth", "peer", "name", dev2, "netns", n2)
            for ns, dev, addr in [(n1, dev1, addr1), (n2, dev2, addr2)]:
                run("ip", "-n", ns, "addr", "add", addr + "/30", "dev", dev)
                run("ip", "-n", ns, "link", "set", dev, "mtu", str(MTU), "up")
        run("ip", "-n", src, "route", "add", "default", "via", "192.0.2.2")
        run("ip", "-n", sink, "route", "add", "default", "via", "198.51.100.1")
        features = {}
        if mode == "gvisor-source-segmented":
            for ns, dev, wanted in ((src, "left", {"tso": "tcp-segmentation-offload", "gso": "generic-segmentation-offload"}),
                                    (router, "rin", {"gro": "generic-receive-offload"})):
                run("ip","netns","exec",ns,"ethtool","-K",dev,*(x for flag in wanted for x in (flag,"off")))
                reported = run("ip","netns","exec",ns,"ethtool","-k",dev)
                for name in wanted.values():
                    if not re.search("^"+name+r": off(?:\s|$)", reported,re.M):
                        raise RuntimeError(f"{dev} {name} was not disabled")
                    features[f"{dev}:{name}"] = "off"
        ingress = work / "ingress.json"
        stop = work / "stop"
        watcher = spawn(scope, ["ip", "netns", "exec", router, sys.executable, SCRIPT, "capture", ingress, stop])
        ready(watcher)
        if mode == "linux":
            run("ip", "netns", "exec", router, "sysctl", "-qw", "net.ipv4.ip_forward=1")
        else:
            fs = work / "rootfs"
            fs.mkdir()
            shutil.copy2(helper, fs / "init")
            (fs / "proc").mkdir()
            config = dict(ociVersion="1.0.2", root=dict(path="rootfs", readonly=True),
                          process=dict(args=["/init"], cwd="/", user=dict(uid=0, gid=0),
                                       capabilities={k: ["CAP_NET_ADMIN"] for k in ("effective", "permitted", "bounding")}),
                          mounts=[dict(destination="/proc", type="proc", source="proc")],
                          linux=dict(namespaces=[dict(type=k) for k in ("pid", "ipc", "uts", "mount")] +
                                     [dict(type="network", path="/var/run/netns/" + router)]))
            (work / "config.json").write_text(json.dumps(config))
            base = [runtime, "--root=" + str(work / "state"), "--ignore-cgroups=true", "--platform=systrap",
                    "--debug=true", "--debug-log=" + str(work / "debug-%COMMAND%.log"), "--log-packets=true",
                    "--gso=" + ("false" if mode == "gvisor-no-host-gso" else "true")]
            scope.callback(unmount_null_netns, work / "state" / "null-netns")
            scope.callback(subprocess.run, [*base, "delete", "--force", "router"], check=False)
            errors = scope.enter_context((work / "runtime.stderr").open("w"))
            ready(spawn(scope, [*base, "run", "--bundle", work, "router"], stderr=errors))
        ready(spawn(scope, ["ip", "netns", "exec", src, sys.executable, SCRIPT, "serve"]))
        try:
            result = json.loads(run("ip", "netns", "exec", sink, sys.executable, SCRIPT, "receive"))
        finally:
            stop.touch()
            if watcher.wait(timeout=3):
                raise RuntimeError("ingress observation failed")
        result["ingress"] = json.loads(ingress.read_text())
        if mode != "linux":
            result["guestLink"] = guest_link(work, result["ingress"])
            result["ingress"].pop("dfGsoPackets", None)
            result["ingress"].pop("packetLengths", None)
            result["features"] = features
        return result


def main(modes: list[str]) -> int:
    records = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    runfiles = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]
    result = dict(source=os.environ["PRODUCT_SHA"], expected=modes, results={}, failures={})
    with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as directory:
        root = Path(directory).resolve()
        release = root / "release"
        runtime = release / "runsc"
        if modes != ["linux"]:
            shutil.copytree(runfiles / "release", release)
            result["runsc_sha256"] = hashlib.sha256(runtime.read_bytes()).hexdigest()
        for mode in modes:
            try:
                result["results"][mode] = compare(mode, root, runtime, runfiles / "test/rbe/forward_gso/router")
            except Exception:
                result["failures"][mode] = traceback.format_exc()
                work = root / mode
                partial = result.setdefault("partial", {}).setdefault(mode, {})
                if (host := work / "ingress.json").is_file():
                    partial["ingress"] = json.loads(host.read_text())
                partial["guestLink"] = guest_link(work, partial.get("ingress", {}))
                if "ingress" in partial:
                    partial["ingress"].pop("dfGsoPackets", None)
                    partial["ingress"].pop("packetLengths", None)
                if (stderr := work / "runtime.stderr").is_file():
                    partial["runtimeError"] = [line[:1200] for line in stderr.read_text(errors="replace").splitlines()
                        if not re.search(r"authorization|api.key|password|bearer|remote.header|token",line,re.I)][-5:]
            (records / "verdict.json").write_text(json.dumps(result, indent=2))
    expected_hash = hashlib.sha256(BLOCK * 16).hexdigest()
    for mode, row in result["results"].items():
        row["complete"] = row["size"] == LENGTH and row["sha256"] == expected_hash
        if mode == "gvisor-source-segmented":
            recv = row["guestLink"]["tcp"].get("recv", {})
            if not (row["complete"] and row["ingress"].get("largestIP", 0) in range(1,MTU+1)
                    and row["ingress"].get("over_mtu", 0) == row["guestLink"]["fragNeededSent"] == 0
                    and recv.get("packets",0) > 0 and recv.get("matchedHostPackets") == recv["packets"]
                    and recv["maxMatchedIPLength"] <= MTU):
                result["failures"][mode] = "the transfer did not establish segmented input"
        elif not row["ingress"].get("tcpv4_gso") or (mode in ("linux", "gvisor-no-host-gso") and not row["complete"]):
            result["failures"][mode] = "missing TCPv4 GSO ingress or incomplete control transfer"
    result["verified"] = not result["failures"] and len(result["results"]) == len(modes)
    (records / "verdict.json").write_text(json.dumps(result, indent=2))
    suite = ET.Element("testsuite", name="ForwardedGSO", tests=str(len(modes)), failures=str(len(result["failures"])))
    for mode in modes:
        case = ET.SubElement(suite, "testcase", classname="ForwardedGSO", name=mode)
        if mode in result["failures"]:
            ET.SubElement(case, "failure").text = result["failures"][mode]
    ET.ElementTree(suite).write(os.environ["XML_OUTPUT_FILE"])
    print(json.dumps(result, indent=2))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["serve"]:
            serve()
        case ["receive"]:
            receive()
        case ["capture", output, stop]:
            capture(Path(output), Path(stop))
        case ["linux"] | ["gvisor-host-gso", "gvisor-no-host-gso"] | ["gvisor-source-segmented"]:
            sys.exit(main(sys.argv[1:]))
        case other:
            raise ValueError(other)
