"""Qualify attachment-owned packet processors in one native Linux checkout."""

import json
import os
from pathlib import Path
import re
import subprocess
from xml.etree import ElementTree

ORIGINAL = "c0e6b2876b97d87503e115d87c905c45926245b9"
PRODUCT = "4524ee0d062148e9e2d66231b58df0267be68d9c"
FD = "//pkg/tcpip/link/fdbased:fdbased_test"

TMP = Path(os.environ["RUNNER_TEMP"])
RESULT = TMP / "fdbased-results"
RESULT.mkdir()
REPORT = {"original": ORIGINAL, "product": PRODUCT, "cases": {}}
RECEIVE = {
    f"TestDeliverPacket/Eth={eth},PayloadLen={size}" for eth in ("true", "false") for size in (100, 1000)
} | {
    "TestDispatchPacketFormat/" + mode for mode in (
        "readVDispatcher", "recvMMsgDispatcher", "readVDispatcherNoEth", "readVDispatcherNoEthIPv6"
    )
} | {
    "TestPacketMMapDeliversUDP/processors=" + str(n) for n in (0, 1, 2)
} | {"TestCloseReleasesStopFDs", "TestNegativeProcessorCount", "TestDetachedReceiveBatch", "TestInjectableAttachment"}
MUXED = "//pkg/tcpip/link/muxed:muxed_test"
REGRESSIONS = {
    "TestDetachedReceiveBatch": "delivered 2 packets from the detached batch, want 0",
    "TestInjectableAttachment": "IsAttached returned false after attaching the dispatcher",
}


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
        r"|detached batch delivered|IsAttached returned|^\s*--- (PASS|FAIL|SKIP):|[Dd]eadlock|ERROR:|error:|panic:|timed out", line)]
    (RESULT / (arm + "-events.txt")).write_text("\n".join(evidence[:160]) + "\n")
    if failure is None:
        ok = result.returncode == 0 and names <= set(typed["PASS"]) and not typed["FAIL"] and not typed["SKIP"]
    else:
        ok = result.returncode == 3 and set(typed["FAIL"]) == names and all(message in output for message in failure)
    REPORT["cases"][arm] = {
        "label": owner, "exit": result.returncode, "names": typed, "xml_names": xml_names,
        "expected": sorted(names), "matched": ok,
    }
    save()
    print(arm, json.dumps(REPORT["cases"][arm]), flush=True)
    if not ok:
        print(output[-16000:], flush=True)


test("receive", FD, RECEIVE)
test("live-save", FD, {"TestLiveSaveKeepsReceivingUDP/Readv", "TestLiveSaveKeepsReceivingUDP/RecvMMsg"})
test("muxed", MUXED, {
    "TestInjectableEndpointMTU", "TestInjectableEndpointRawDispatch",
    "TestInjectableEndpointDispatch", "TestInjectableEndpointDispatchHdrOnly",
})
nogo = "//pkg/tcpip/link/fdbased:fdbased_nogo"
nogo_events = TMP / "fdbased-nogo.bep.json"
run = subprocess.run([
    str(TMP / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g",
    "test", "--config=x86_64", "--jobs=3", "--strategy=TestRunner=local",
    "--nocache_test_results", "--test_output=errors", "--color=no",
    "--build_event_json_file=" + str(nogo_events), nogo,
], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
summaries = []
if nogo_events.exists():
    for line in nogo_events.read_text().splitlines():
        event = json.loads(line)
        if event.get("id", {}).get("testSummary", {}).get("label") == nogo:
            summaries.append(event.get("testSummary", {}).get("overallStatus"))
REPORT["cases"]["nogo"] = {"label": nogo, "exit": run.returncode, "status": summaries,
                             "matched": run.returncode == 0 and summaries == ["PASSED"]}
save()
if not REPORT["cases"]["nogo"]["matched"]:
    print(run.stdout.decode(errors="replace")[-10000:], flush=True)
test_path = Path("pkg/tcpip/link/fdbased/endpoint_test.go")
reviewed = test_path.read_text()
def declaration(start):
    begin = reviewed.index(start)
    return reviewed[begin:reviewed.index("\n}", begin) + 2]


definitions = [
    declaration("func (d *fakeNetworkDispatcher) releasePackets()"),
    reviewed[reviewed.index("// pausedBatchReader "):reviewed.index("func TestInjectableAttachment(")],
    declaration("func TestInjectableAttachment("),
]

# Keep the same fork-only fixture and its BUILD declarations across both
# source arms. In particular, restore the source-specific dispatch signatures
# in the original package's own tests.
changed = git("diff", "--name-only", ORIGINAL, PRODUCT, "--", "pkg/tcpip/link/fdbased").decode().splitlines()
assert changed and all(n.endswith(".go") for n in changed)
git("restore", "--source=" + ORIGINAL, "--worktree", "--", *changed)
git("diff", "--exit-code", ORIGINAL, "--", "pkg/tcpip/link/fdbased",
    ":!pkg/tcpip/link/fdbased/BUILD", ":!pkg/tcpip/link/fdbased/packet_ring_linux_test.go",
    ":!pkg/tcpip/link/fdbased/receive_lifetime_test.go")
with test_path.open("a") as baseline_test:
    baseline_test.write("\n\n" + "\n\n".join(definitions) + "\n")
test("prior-attachment", FD, set(REGRESSIONS), failure=REGRESSIONS.values())
assert all(result["matched"] for result in REPORT["cases"].values()), REPORT["cases"]
