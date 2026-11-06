"""Qualify attachment-owned packet processors in one native Linux checkout."""

import json
import os
from pathlib import Path
import re
import subprocess
from xml.etree import ElementTree

ORIGINAL = "4e0324239c7e64cb4d1cb12e804603aa85a963f3"
PRODUCT = "c32ea8f499a5b52cc5ff78f7bf1b3d2f74f4851e"
FD = "//pkg/tcpip/link/fdbased:fdbased_test"

TMP = Path(os.environ["RUNNER_TEMP"])
RESULT = TMP / "fdbased-results"
RESULT.mkdir()
REPORT = {"original": ORIGINAL, "product": PRODUCT, "cases": {}}
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
        try:
            xml_names = [case.get("name") for case in ElementTree.fromstring(xml).iter("testcase")]
        except ElementTree.ParseError:
            pass  # A crashing comparison still has its Bazel exit and stdout.
    verbose = re.findall(r"^\s*--- (PASS|FAIL|SKIP): ([^\s(]+)", output, re.MULTILINE)
    typed = {state: sorted({n for s, n in verbose if s == state}) for state in ("PASS", "FAIL", "SKIP")}
    evidence = [line for line in output.splitlines() if re.search(
        r"received UDP|checkpoint contains|receiver exited|did not reach its idle poll|negative refcount|PACKET_RING|configured [0-9]+ processors for [0-9]+ requested|Wait returned while DeliverNetworkPacket"
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


test("receive", FD, RECEIVE | {"TestNegativeProcessorCount"})
test("live-save", FD, {"TestLiveSaveKeepsReceivingUDP/Readv", "TestLiveSaveKeepsReceivingUDP/RecvMMsg"})
for mode in ("Readv", "RecvMMsg"):
    test("changed-move-" + mode, FD, {"TestMove" + mode})

# Keep the same fork-only fixture and its BUILD declarations across both
# source arms. In particular, restore the source-specific dispatch signatures
# in the original package's own tests.
changed = git("diff", "--name-only", ORIGINAL, PRODUCT, "--", "pkg/tcpip/link/fdbased").decode().splitlines()
assert changed and all(n.endswith(".go") for n in changed)
git("restore", "--source=" + ORIGINAL, "--worktree", "--", *changed)
git("diff", "--exit-code", ORIGINAL, "--", "pkg/tcpip/link/fdbased",
    ":!pkg/tcpip/link/fdbased/BUILD", ":!pkg/tcpip/link/fdbased/packet_ring_linux_test.go",
    ":!pkg/tcpip/link/fdbased/receive_lifetime_test.go")
for mode in ("Readv", "RecvMMsg"):
    test("original-move-" + mode, FD, {"TestMove" + mode})

# Record any shared pre-existing move failure separately. A green test
# requires receipt of two labeled packets after the new reader's idle poll.
for mode in ("Readv", "RecvMMsg"):
    old, new = [REPORT["cases"][v + "-move-" + mode] for v in ("original", "changed")]
    old_log, new_log = [(RESULT / (v + "-move-" + mode + "-events.txt")).read_text() for v in ("original", "changed")]
    common = [msg for msg in ("receiver exited before the next UDP packet", "read i/o timeout",
                              "negative refcount", "receiver did not reach its idle poll")
              if msg in old_log and msg in new_log]
    REPORT.setdefault("move", {})[mode] = {"both_delivered": old["matched"] and new["matched"],
                                            "same_failure": common, "new_delivered": new["matched"]}
save()
assert REPORT["cases"]["receive"]["matched"] and REPORT["cases"]["live-save"]["matched"]
assert all(v["new_delivered"] or v["same_failure"] for v in REPORT["move"].values()), REPORT["move"]
