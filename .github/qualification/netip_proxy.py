"""Exercise the changed proxy and interface tests on the rebased source."""
from pathlib import Path
import os
import selectors
import socket
import re
import subprocess

import native
import previous


NFT = "//pkg/tcpip/nftables:nftables_test"
OWNERS = {
    NFT: ["TestMetaOutputInterface", "TestMasqueradeOutputInterface",
          "TestFIBOutputInterface", "TestCompatAddrtypeLimitIfaceOut"],
    "//pkg/tcpip/network/ipv4:ipv4_test": ["TestDropOtherHostPackets", "TestForwardingTCPChecksum"],
    "//pkg/tcpip/network/ipv6:ipv6_test": ["TestDropOtherHostPackets", "TestForwardingTCPChecksum"],
    "//pkg/tcpip/stack:bridge_test": ["TestBridgeDispatchOnlyForLocalOrMulticast", "TestBridgePortAddressChange",
                                    "TestBridgeDoesNotLearnLocalAddresses", "TestBridgeARPRequests"],
    "//pkg/tcpip/tests/integration:loopback_test": ["TestPerNICExternalIPv4LoopbackTraffic"],
    "//pkg/tcpip/stack:stack_test": ["TestPacketDataReadToPreservesHeaders"],
    "//runsc/gofer/net:netgofer_test": [
        "TestRunProxyModeAllowLoopback", "TestSpliceAndCloseWrite", "TestRunProxyModeEndToEnd", "TestRunProxyModeWithSeccomp",
        "TestProxyLimitsConnections", "TestProxyRejectsDisallowedDestinations",
        "TestProxyLimitsUDPConnections", "TestProxyUDPIdleTimeout",
        "TestOutboundUDPDialerDisablesBroadcast", "TestNetgoferEarlyValidationClosesUDSFD",
    ],
}
SOURCE = "3aaa8098b633dab610b06e4c6d92122c24037451"
TREE = "bfc731bd74b82504fef6569caa2367459828ad32"
BASE = "2a653cea7b2054a2dc9c6419337d3de8031275d6"


def check(label, text, xml, row):
    passed = set(re.findall(r"(?m)^\s*--- PASS: (\S+) \(", text))
    failed = re.findall(r"(?m)^\s*--- FAIL: (\S+) \(", text)
    skipped = re.findall(r"(?m)^\s*--- SKIP: (\S+) \(", text)
    required = set(OWNERS[label])
    if label == NFT:
        for rule in ("masq", "MASQUERADE"):
            for scenario in ("no_route", "no_route_other_interface", "route", "no_output_interface"):
                required.add(f"TestMasqueradeOutputInterface/{rule}/{scenario}")
    row.update(passed=sorted(passed), failed=failed, skipped=skipped)
    assert not failed and required <= passed, (failed, sorted(required - passed))
    assert not any(c.find("error") is not None or c.find("failure") is not None
                   for c in xml.iter("testcase"))



def build_proxy():
    flags = ["--config=x86_64", "--jobs=4", "--lockfile_mode=off", "--remote_executor=", "--remote_cache=",
             "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local", "--strategy=GoStdlib=processwrapper-sandbox",
             "--strategy=GoStdlibList=processwrapper-sandbox"]
    for attempt in range(24):
        cmd = [str(native.WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
               "build", *flags, *["--distdir=" + str(d) for d in sorted(previous.DIRS)], "//test/benchmarks/tcp:tcp_proxy"]
        built = subprocess.run(cmd, capture_output=True, timeout=600)
        if built.returncode == 0:
            break
        logs = (built.stdout + built.stderr).decode(errors="replace")
        if not previous.recover(logs):
            raise RuntimeError(logs[-3500:])
    else:
        raise RuntimeError("public dependency recovery exhausted")
    outputs = [path for path in Path("bazel-bin/test/benchmarks/tcp").glob("**/tcp_proxy")
               if path.is_file() and os.access(path, os.X_OK)]
    if len(outputs) != 1:
        raise RuntimeError(str(outputs))
    return outputs[0].resolve()


def recv_exact(stream, expected):
    buf = bytearray()
    while len(buf) < len(expected):
        chunk = stream.recv(len(expected) - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    assert buf == expected, (buf, expected)


def proxy_controls():
    exe = build_proxy()
    controls = native.REPORT["proxy"] = {"exe": str(exe), "cases": {}}
    for name, args, error in [
        ("listen port overflow", ["-port=65536", "-forward=127.0.0.1:10"], "invalid listen port"),
        ("netstack forward port overflow", ["-port=80", "-client", "-forward=127.0.0.1:65536"], "invalid forwarding target"),
        ("host forward port overflow", ["-port=80", "-forward=127.0.0.1:65536"], "invalid forwarding port"),
        ("netstack scoped IPv6", ["-port=80", "-client", "-forward=[fe80::1%lo]:80"], "omit the zone"),
    ]:
        p = subprocess.run([exe, *args], capture_output=True, timeout=8)
        text = p.stderr.decode(errors="replace")
        controls["cases"][name] = {"exit": p.returncode, "stderr": text[-500:]}
        assert p.returncode == 1 and error in text, (args, p.returncode, text[-500:])
    with socket.socket() as reserve, socket.socket() as peer:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
        peer.bind(("127.0.0.1", 0))
        peer.listen()
        peer.settimeout(10)
        reserve.close()
        proxy = subprocess.Popen([exe, "-port=" + str(port), "-forward=127.0.0.1:" + str(peer.getsockname()[1])],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            with peer.accept()[0] as remote, selectors.DefaultSelector() as ready:
                remote.settimeout(5)
                ready.register(proxy.stderr, selectors.EVENT_READ)
                assert ready.select(8), "proxy did not bind"
                line = proxy.stderr.readline()
                assert b"ready." in line, line
                with socket.create_connection(("127.0.0.1", port), 5) as client:
                    client.sendall(b"netip-to-peer")
                    recv_exact(remote, b"netip-to-peer")
                    remote.sendall(b"netip-to-client")
                    recv_exact(client, b"netip-to-client")
                    controls["cases"]["host bidirectional transfer"] = "13 bytes to peer, 15 bytes to client"
        finally:
            proxy.terminate()
            proxy.communicate(timeout=10)


assert os.uname().machine == "x86_64" and os.geteuid() != 0
for rev, expected in (("HEAD", SOURCE), ("HEAD^{tree}", TREE), ("HEAD^", BASE)):
    assert subprocess.check_output(["git", "rev-parse", rev], text=True).strip() == expected
assert not subprocess.check_output(["git", "diff", "--name-only"])
native.REPORT = {"product": SOURCE, "tree": TREE, "base": BASE, "commands": {},
                 "tests": {}, "inputs": {}, "verified": False}
previous.OUT = native.WORK
previous.REPORT["assets"] = native.REPORT["inputs"]
native.check_case = check
names = sorted({name for required in OWNERS.values() for name in required})
try:
    native.run("owners", list(OWNERS), [
        "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1",
        "--test_arg=-test.run=^(" + "|".join(names) + ")$",
    ], 105)
    assert set(native.REPORT["tests"]) == set(OWNERS)
    assert all(row["verified"] for row in native.REPORT["tests"].values())
    assert all(cmd["exit"] == 0 or cmd.get("recovered") for cmd in native.REPORT["commands"].values())
    proxy_controls()
    native.REPORT["verified"] = True
finally:
    native.save()
