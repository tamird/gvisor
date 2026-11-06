"""Compare real receive workers in one native Linux checkout."""

import json
import os
from pathlib import Path
import re
import subprocess
from xml.etree import ElementTree

ORIGINAL = "4e0324239c7e64cb4d1cb12e804603aa85a963f3"
JOIN = "e5f9acc7624cbb413011c854b5f65db67121678d"
STACK = "39d28d4ef5d4a11aa5f8756d62bf99e1cd02816d"
FD = "//pkg/tcpip/link/fdbased:fdbased_test"
PRODUCTION = ("pkg/tcpip/link/fdbased/endpoint.go", "pkg/tcpip/link/fdbased/processors.go")
TMP = Path(os.environ["RUNNER_TEMP"])
RESULT = TMP / "fdbased-results"
RESULT.mkdir()
REPORT = {"original": ORIGINAL, "join": JOIN, "stack": STACK, "cases": {}}
RECEIVE = {
    "TestCloseBeforeAttach/" + mode for mode in ("Readv", "RecvMMsg")
} | {
    "TestDispatchPacketFormat/" + mode for mode in (
        "readVDispatcher", "recvMMsgDispatcher", "readVDispatcherNoEth", "readVDispatcherNoEthIPv6"
    )
} | {
    "TestPacketMMapDeliversUDP/processors=" + str(n) for n in (0, 1, 2)
} | {"TestPacketMMapBeforeAttach"}


def save():
    (RESULT / "summary.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def git(*args, input=None):
    return subprocess.run(["git", *args], check=True, input=input, stdout=subprocess.PIPE).stdout


def test(arm, owner, names, *, failure=None):
    roots = sorted({name.split("/")[0] for name in names})
    args = [
        str(TMP / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g",
        "test", "--config=x86_64", "--jobs=3", "--strategy=TestRunner=local",
        "--nocache_test_results", "--test_timeout=150", "--color=no", "--test_output=all",
        "--test_arg=-test.v", "--run_under=" + os.environ["namespace"],
        "--test_filter=^(" + "|".join(roots) + ")$", owner,
    ]
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = result.stdout.decode(errors="replace")
    test_file = Path("bazel-testlogs") / owner[2:].replace(":", "/") / "test.xml"
    xml_names = []
    if test_file.is_file():
        xml = test_file.read_bytes()
        (RESULT / (arm + ".xml")).write_bytes(xml)
        xml_names = [case.get("name") for case in ElementTree.fromstring(xml).iter("testcase")]
    verbose = re.findall(r"^\s*--- (PASS|FAIL|SKIP): ([^\s(]+)", output, re.MULTILINE)
    typed = {state: sorted({n for s, n in verbose if s == state}) for state in ("PASS", "FAIL", "SKIP")}
    evidence = [line for line in output.splitlines() if re.search(
        r"PACKET_RING|configured [0-9]+ processors for [0-9]+ requested|Wait returned while DeliverNetworkPacket"
        r"|^\s*--- (PASS|FAIL|SKIP):|[Dd]eadlock|ERROR:|error:|panic:|timed out", line)]
    (RESULT / (arm + "-events.txt")).write_text("\n".join(evidence[:160]) + "\n")
    if failure is None:
        ok = result.returncode == 0 and names <= set(typed["PASS"]) and not typed["FAIL"] and not typed["SKIP"]
    else:
        ok = result.returncode == 3 and set(typed["FAIL"]) == names and failure in output
    REPORT["cases"][arm] = {
        "label": owner, "exit": result.returncode, "names": typed, "xml_names": xml_names,
        "expected": sorted(names), "matched": ok,
    }
    save()
    print(arm, json.dumps(REPORT["cases"][arm]), flush=True)
    if not ok:
        print(output[-16000:], flush=True)


# Only add the new independent test to the original product. The original
# packet cases run first, with both production files at their exact old blobs.
git("restore", "--source=" + ORIGINAL, "--worktree", "--", *PRODUCTION)
git("diff", "--exit-code", ORIGINAL, "--", "pkg/tcpip/link/fdbased",
    ":!pkg/tcpip/link/fdbased/endpoint_test.go", ":!pkg/tcpip/link/fdbased/packet_ring_linux_test.go",
    ":!pkg/tcpip/link/fdbased/BUILD")
test("original-receive", FD, RECEIVE)
test("original-join", FD, {"TestWaitForPacketDelivery"},
     failure="Wait returned while DeliverNetworkPacket was still running")

git("restore", "--source=" + JOIN, "--worktree", "--", *PRODUCTION)
test("joined", FD, {"TestWaitForPacketDelivery"})

# #17103 owns both stack changes: detach and the explicit link Wait occur
# after unlocking Stack.mu. Replay that exact four-file patch before its
# reentrant NIC lookup test and the separate real packet endpoint test.
patch = git("diff", STACK + "^", STACK, "--", "pkg/tcpip/stack")
git("apply", "--check", "-", input=patch)
git("apply", "-", input=patch)
test("stack-lookup", "//pkg/tcpip/stack:stack_test", {"TestStackWaitAllowsReceiveLookup"})
test("stack-join", FD, {"TestWaitForPacketDelivery"})
assert all(c["matched"] for c in REPORT["cases"].values()), REPORT
