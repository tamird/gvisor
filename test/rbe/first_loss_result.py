"""Retain only the named TCP test results, measured events and report identities."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

out, code = Path(sys.argv[1]), int(sys.argv[2])
owner = os.environ["QUALIFICATION_TARGETS"]
expected = {'//pkg/tcpip/transport/tcp/test/e2e:tcp_test': ['TestPartialGSOProgress', 'TestPartialGSOProgress/ack', 'TestPartialGSOProgress/timeout', 'TestPartialGSOProgress/restore'], '//pkg/tcpip/transport/tcp:tcp_test': ['TestCleanup', 'TestCubicCongestionAvoidanceLimitsGrowth', 'TestCubicCongestionAvoidanceLimitsGrowth/new_credit', 'TestCubicCongestionAvoidanceLimitsGrowth/retained_credit', 'TestCubicCongestionAvoidanceNeedsAcknowledgments', 'TestCubicFastConvergenceStartsAtCurrentWindow', 'TestCubicFriendlyEstimateAbovePriorWindow', 'TestCubicFriendlyEstimateUsesACKs', 'TestCubicFriendlyRegionConsumesOldCredit', 'TestCubicHyStartInitializesCongestionAvoidance', 'TestCubicRTOPreservesPriorWindow', 'TestCubicRecoveryDiscardsACKCredit', 'TestCubicSlowStartPreservesExcessACKs', 'TestCubicWindowRestartPreservesPeak', 'TestCubicWindowRestartPreservesPeak/fast', 'TestCubicWindowRestartPreservesPeak/timeout', 'TestHyStartAckTrainOK', 'TestHyStartAckTrainTooSpread', 'TestHyStartAckTrainUsesIngressTime', 'TestHyStartDelayOK', 'TestHyStartDelay_BelowThresh', 'TestRACKClockResolutionLossBoundaryIsExclusive', 'TestRACKClockResolutionPreventsPrematureLoss', 'TestRACKClockResolutionPreventsPrematureLoss/quantization_allowance', 'TestRACKClockResolutionPreventsPrematureLoss/unspecified_resolution', 'TestRACKDetectLossSteadyStateDoesNotAllocate', 'TestRACKEqualTransmitTimeOrdersByEndSequence', 'TestRACKInitialDSACKWindow', 'TestRACKInitialDSACKWindow/high_isn', 'TestRACKInitialDSACKWindow/low_isn', 'TestRACKInitialDSACKWindow/sequence_wrap', 'TestRACKInitialDeliverySequence', 'TestRACKInitialDeliverySequence/high_isn', 'TestRACKInitialDeliverySequence/low_isn', 'TestRACKInitialDeliverySequence/sequence_wrap', 'TestRACKQuantizedClockPreventsPrematureLossAndSchedulesRecheck', 'TestRACKReorderTimerDeclaresLossOnFirstExpiry', 'TestRACKZeroClockResolutionPreservesLossBoundary', 'TestRenoCongestionAvoidanceCarriesExcessACKCredit', 'TestRenoCongestionAvoidanceConsumesACKCredit', 'TestSegmentMerge', 'TestSendBufferStateSnapshot'], '//pkg/tcpip/transport/tcp:tcp_nogo': ['pkg/tcpip/transport/tcp/tcp_nogo']}[owner]
if owner.endswith("/e2e:tcp_test"):
    expected = ['TestPartialGSOProgress', 'TestPartialGSOProgress/ack', 'TestPartialGSOProgress/timeout', 'TestPartialGSOProgress/restore', 'TestPartialGSOProgress/fresh_refusal', 'TestPartialGSOProgress/retransmit_refusal']
product = '2315c94dd5b268f5329d89c763123c5df8219cc3'
def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()
result = {"status": "FAIL", "source": git("rev-parse", "HEAD"), "product": product, "owner": owner, "native_exit": code}
stage = "source"
try:
    assert result["source"] == os.environ["QUALIFICATION_COMMIT"]
    assert git("rev-parse", "HEAD^") == product
    assert git("rev-parse", product + "^") == "5edf5d13c7a6749df466add29d8811260edb4cda"
    subprocess.run(["git", "diff", "--exit-code", product, "HEAD", "--", "pkg"], check=True)
    rows = [json.loads(line) for line in (out / "events.jsonl").open()]
    stage = "BEP"
    attempts = [r for r in rows if r.get("id", {}).get("testResult", {}).get("label") == owner]
    result["attempts"] = len(attempts)
    result["failure_categories"] = sorted({reason if re.fullmatch(r"[A-Z_]{1,40}", reason) else "UNSPECIFIED" for r in rows if "aborted" in r for reason in [r["aborted"].get("reason", "UNSPECIFIED")]})
    assert len(attempts) == 1
    test = attempts[0]["testResult"]
    result["remote"] = test.get("executionInfo", {}).get("strategy") == "remote"
    result["cached"] = bool(test.get("cachedLocally") or test.get("executionInfo", {}).get("cachedRemotely"))
    result["child"] = next((r["started"].get("uuid") for r in rows if "started" in r), None)
    assert result["remote"] and not result["cached"]
    result["test_status"] = test["status"]
    stage = "reports"
    files = {f["name"]: f for f in test["testActionOutput"] if f["name"] in {"test.log", "test.xml"}}
    assert set(files) == {"test.log", "test.xml"}
    content = {}
    result["reports"] = {}
    for name, f in files.items():
        file = Path("bazel-testlogs",owner[2:].replace(":", "/"),name)
        data = file.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert f.get("digest", digest) == digest and int(f.get("length", len(data))) == len(data)
        content[name] = data
        result["reports"][name] = {"sha256": digest, "bytes": len(data)}
    cases = ET.fromstring(content["test.xml"]).findall(".//testcase")
    result["cases"] = [{"name": c.attrib["name"], "passed": all(c.find(t) is None for t in ("skipped", "error", "failure"))} for c in cases]
    assert {c["name"] for c in result["cases"]} == set(expected) and len(cases) == len(expected)
    if owner.endswith(":tcp_nogo"):
        log = content["test.log"].decode()
        result["target_arch_mentions"] = [arch for arch in ("arm64", "amd64") if arch in log]
        assert len(result["target_arch_mentions"]) == 2
    log = content["test.log"].decode(errors="replace")
    result["prefix_observations"] = [[int(x) for x in m] for m in re.findall(r"sender counts rejected bytes: SndNxt=(\d+) Outstanding=(\d+) want (\d+),1", log)]
    result["flight_disagreements"] = [[int(x) for x in m] for m in re.findall(r"SND.NXT = \+(\d+), outstanding = (\d+); want \+(\d+), (\d+)", log)]
    result["counter_disagreements"] = {name: [[int(x) for x in m] for m in re.findall(re.escape(name)+r" = (\d+), want (\d+)", log)] for name in ("route errors after the duplicate ACK", "route errors", "timeouts", "retransmission attempts")}

    result["status"] = "PASS" if all(c["passed"] for c in result["cases"]) and not code and result["test_status"]=="PASSED" else "FAIL"
except (AssertionError, KeyError, ValueError, OSError, StopIteration, ET.ParseError, subprocess.CalledProcessError) as err:
    result["error"] = {"stage":stage,"kind":type(err).__name__}
finally:
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
sys.exit(int(result["status"]!="PASS"))
