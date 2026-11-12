from pathlib import Path
import shutil
import subprocess
import sys

target = Path("pkg/tcpip/transport/tcp/rack.go")
original = target.read_bytes()
start = original.index(b"\ts.rc.tlpRxtOut = false\n\ts.cc.HandleLossDetected()")
end = original.index(b"\ts.ep.stack.Stats().TCP.TLPRecovery.Increment()", start)
response = original[start:end]
assert response.count(b"\ts.cc.HandleLossDetected()") == 1
assert b"s.state = tcpip.Open" in response and b"s.state = tcpip.Disorder" in response
primed = response.replace(b"\ts.cc.HandleLossDetected()", b"\ts.capturePipePrev()\n\ts.cc.HandleLossDetected()")
legacy = b"\ts.rc.tlpRxtOut = false\n\ts.capturePipePrev()\n\ts.cc.HandleLossDetected()\n\ts.enterRecovery()\n\ts.leaveRecovery()\n"
work = Path(sys.argv[1])
directory = work / "receipts"
directory.mkdir(exist_ok=True)
labels = ["tcp_rack_test", "tcp_sack_test"]
base = "//pkg/tcpip/transport/tcp/test/e2e:"
source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
for name, replacement in (("combined", response), ("primed-direct", primed), ("primed-legacy", legacy)):
    arm = directory / name
    arm.mkdir()
    target.write_bytes(original[:start] + replacement + original[end:])
    blob = subprocess.check_output(["git", "hash-object", str(target)], text=True).strip()
    (arm / "source.txt").write_text(f"{source}\n{blob} {target}\n")
    (arm / "rack.diff").write_bytes(subprocess.check_output(["git", "diff", "--", str(target)]))
    selected = labels if name == "combined" else ["tcp_sack_test"]
    command = ["bazelisk", "--bazelrc=" + str(work / "client.rc"), "test", "--config=x86_64",
               "--nocache_test_results", "--test_output=errors", "--test_arg=-test.v"]
    if name != "combined":
        command.append("--test_filter=^TestTailLossProbeResponseIsKept$")
    code = subprocess.run(command + [base + label for label in selected]).returncode
    (arm / "bazel-exit.txt").write_text(f"{code}\n")
    for label in selected:
        result = Path("bazel-testlogs/pkg/tcpip/transport/tcp/test/e2e") / label
        for ext in ("xml", "log"):
            shutil.copyfile(result / ("test." + ext), arm / (label + "." + ext))
target.write_bytes(original)

for name in ("combined", "primed-direct", "primed-legacy"):
    arm = directory / name
    code = int((arm / "bazel-exit.txt").read_text())
    sack = (arm / "tcp_sack_test.log").read_text()
    if name == "primed-legacy":
        assert code == 3, f"{name}: expected a Bazel test failure, got {code}"
        assert "--- FAIL: TestTailLossProbeResponseIsKept" in sack, f"{name}: missing expected test failure"
        assert "ssthresh after a loss repaired by a tail loss probe" in sack and "want 2" in sack, f"{name}: wrong failure"
    else:
        assert code == 0 and "--- PASS: TestTailLossProbeResponseIsKept" in sack, f"{name}: missing passing loss-response test, Bazel {code}"
        if name == "combined":
            rack = (arm / "tcp_rack_test.log").read_text()
            for case in ("TestRACKTLPRecovery", "TestNoTLPRecoveryOnDSACK", "TestRACKOnePacketTailLoss"):
                assert "--- PASS: " + case in rack, f"{name}: missing {case}"
            for case in ("TestDetectSpuriousRecoveryWithRTO", "TestEifelGenuineLossAfterSpuriousDetection"):
                assert "--- PASS: " + case in sack, f"{name}: missing {case}"
print("All expected outcomes and the genuine-loss threshold control verified.")
