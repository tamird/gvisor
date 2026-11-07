"""Run the same occupied-worker test before and after the EOF handoff."""

import hashlib
import json
import os
from pathlib import Path
import traceback
import platform
import subprocess
import xml.etree.ElementTree as ET


BASE = "fbad38ea661236e230a355fde5d0f87d9cac44bb"
FIXED = "3c5c3bf063e63d1f8146feb5e5f33ae99a1623d8"
ROOT = Path(os.environ["RUNNER_TEMP"]) / "netgofer-peer-delivery"
FD = "//pkg/tcpip/link/fdbased:fdbased_test"
NET = "//runsc/gofer/net:netgofer_test"
TEST = "pkg/tcpip/link/fdbased/endpoint_test.go"
NET_TEST = Path("runsc/gofer/net/netgofer_test.go")
INLINE_SHA = "6d4deaa657eb8bf170171089979c14f03acdac7c10c8db5113ae1f270e306337"
PAIR = ("TestSeqpacketWorkerShutdown/PeerEOF", "TestSeqpacketWorkerShutdown/DetachAfterEOF")
PROXY = ("TestSpliceAndCloseWrite/half_close_and_bidirectional_copy", "TestSpliceAndCloseWrite/aborts_on_transport_error",
         "TestSpliceAndCloseWrite/close_write_fallback", "TestRunProxyModeEndToEnd", "TestRunProxyModeWithSeccomp",
         "TestProxyLimitsConnections", "TestProxyRejectsDisallowedDestinations/ipv4_current_network_subnet",
         "TestProxyRejectsDisallowedDestinations/ipv4_link_local_unicast",
         "TestProxyRejectsDisallowedDestinations/ipv4_local_interface_ip",
         "TestProxyRejectsDisallowedDestinations/ipv6_link_local_unicast",
         "TestNetgoferEarlyValidationClosesUDSFD", "TestNetgoferFinalFrameBehindEmptyRecords")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def digest(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def check_case(name: str, labels: tuple[str, ...], record: dict, *, baseline: bool) -> None:
    bep = ROOT / (name + ".bep")
    log = ROOT / (name + ".log")
    args = ["bazel", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "test",
            "--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--local_resources=memory=8192",
            "--spawn_strategy=processwrapper-sandbox,local", "--remote_executor=", "--remote_cache=",
            "--bes_backend=", "--strategy=TestRunner=local", "--nocache_test_results",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v", "--test_timeout=150",
            "--test_output=all", "--build_event_json_file=" + str(bep)]
    if baseline:
        args.append("--test_filter=^TestSeqpacketWorkerShutdown$")
    with log.open("w") as f:
        process = subprocess.run([*args, *labels], stdout=f, stderr=subprocess.STDOUT)
    record.update(exit=process.returncode, owners={})
    events = [json.loads(line) for line in bep.read_text().splitlines()]
    failures = []
    for label in labels:
        try:
            results = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label") == label]
            assert len(results) == 1, (label, "TestRunner count", len(results), log.read_text(errors="replace")[-4800:])
            result, = results
            owner = record["owners"][label] = {"status": result["status"], "strategy": result.get("executionInfo", {}).get("strategy"),
                                                 "cached": result.get("cachedLocally", False)}
            assert not owner["cached"] and owner["strategy"] == "local", (label, owner)
            assert owner["status"] == ("FAILED" if baseline else "PASSED"), (label, owner, log.read_text(errors="replace")[-4800:])
            pkg, target = label[2:].split(":")
            output = Path("bazel-testlogs") / pkg / target
            raw = (output / "test.log").read_bytes()
            xml = (output / "test.xml").read_bytes()
            names = {}
            for case in ET.fromstring(xml).iter("testcase"):
                head, sep, sub = case.attrib["name"].partition("/")
                case_name = head.rsplit(".", 1)[-1] + sep + sub
                names[case_name] = ("SKIP" if case.find("skipped") is not None else
                                    "FAIL" if case.find("failure") is not None or case.find("error") is not None else "PASS")
            go = raw.decode(errors="replace")
            required = PAIR if label == FD else PROXY
            if label == FD and not baseline:
                required += ("TestDeliverPacket/Eth=true,PayloadLen=100", "TestDispatchPacketFormat/recvMMsgDispatcher")
            if label == FD:
                required += ("TestSeqpacketWorkerShutdown/AdministrativeStop",)
            owner.update(xml=names, log_sha256=digest(raw), xml_sha256=digest(xml))
            for test in required:
                expected = "FAIL" if baseline and test in PAIR else "PASS"
                assert names.get(test) == expected and ("--- " + expected + ": " + test + " ") in go, (label, test, names)
            if baseline:
                for message in ("ran before the occupied worker returned", "delivered packets = [1], want [1 2]"):
                    assert message in go, (label, message, go[-4000:])
                owner["early_notification"] = True
                owner["peer_delivered"] = [1]
            if label == NET:
                assert "QUEUE empty=32 delivered=1 marker=5a ended=true" in go
                owner["inline_empty_records"] = 32
            print(name, label, owner["status"], {t: names[t] for t in required}, flush=True)
        except (AssertionError, OSError, KeyError, ET.ParseError, ValueError) as exc:
            failures.append(label)
            record["owners"].setdefault(label, {})["error"] = type(exc).__name__
            traceback.print_exc()
    assert not failures, failures
    assert process.returncode == (3 if baseline else 0), record


def main() -> None:
    os.environ.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.directory",
                      GIT_CONFIG_VALUE_0=os.environ["GITHUB_WORKSPACE"])
    assert platform.machine() == "x86_64"
    assert git("rev-parse", "HEAD^") == FIXED and git("rev-parse", "HEAD^^") == BASE
    assert git("diff", "--name-only", BASE, FIXED).splitlines() == [
        "pkg/tcpip/link/fdbased/endpoint.go", TEST, "runsc/gofer/net/netgofer.go"]
    assert subprocess.check_output(["bazel", "--version"], text=True).strip() == "bazel 9.3.1"
    inline = (ROOT / "inline.txt").read_bytes()
    assert digest(inline) == INLINE_SHA
    receipt = {"source": FIXED, "base": BASE, "arch": platform.machine(), "inline_fixture_sha256": INLINE_SHA, "cases": {}}
    test_source = subprocess.check_output(["git", "show", FIXED + ":" + TEST])
    receipt["test_sha256"] = digest(test_source)
    for arm, source, labels in (("before", BASE, (FD,)), ("after", FIXED, (FD, NET))):
        case = receipt["cases"][arm] = {"verified": False}
        try:
            git("switch", "--detach", source)
            if arm == "before":
                git("restore", "--source=" + FIXED, "--worktree", "--", TEST)
            else:
                with NET_TEST.open("ab") as f:
                    f.write(b"\n" + inline)
            assert Path(TEST).read_bytes() == test_source
            check_case(arm, labels, case, baseline=arm == "before")
            case["verified"] = True
        except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as exc:
            case["error"] = type(exc).__name__
            traceback.print_exc()
        finally:
            (ROOT / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
            git("restore", "--worktree", "--", TEST, str(NET_TEST))
    receipt["verified"] = all(case["verified"] for case in receipt["cases"].values())
    (ROOT / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    assert receipt["verified"], "one or more source arms did not qualify"



if __name__ == "__main__":
    main()
