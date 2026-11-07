"""Run the same occupied-worker test before and after the EOF handoff."""

import hashlib
import json
import os
from pathlib import Path
import traceback
from urllib.parse import urlsplit, unquote
import platform
import subprocess
import xml.etree.ElementTree as ET


BASE = "8a120ab6c04ad9b8007be155d16ce0f98b0b87df"
FIXED = "c44cce1675005074893ef5c1fcc34af37f1df726"
ROOT = Path(os.environ["RUNNER_TEMP"]) / "netgofer-peer-delivery"
FD = "//pkg/tcpip/link/fdbased:fdbased_test"
TEST = "pkg/tcpip/link/fdbased/endpoint_test.go"
PAIR = ("TestSeqpacketWorkerShutdown/PeerEOF", "TestSeqpacketWorkerShutdown/DetachAfterEOF")


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
                names[case_name] = ("SKIP" if case.find("skipped") is not None or case.get("status") in ("notrun", "suppressed") else
                                    "FAIL" if case.find("failure") is not None or case.find("error") is not None else "PASS")
            go = raw.decode(errors="replace")
            required = PAIR + ("TestSeqpacketWorkerShutdown/AdministrativeStop",)
            if not baseline:
                required += ("TestDispatchPacketFormat/recvMMsgDispatcher", "TestDetachedReceiveBatch", "TestInjectableAttachment")
            declared = {f["name"]: f for f in result["testActionOutput"] if f["name"] in ("test.log", "test.xml")}
            for filename, data in (("test.log", raw), ("test.xml", xml)):
                f = declared[filename]
                if f.get("digest"):
                    assert f["digest"] == digest(data)
                else:
                    uri = urlsplit(f["uri"])
                    assert uri.scheme == "file" and not uri.netloc and Path(unquote(uri.path)).resolve() == (output/filename).resolve()
            owner.update(xml=names, log_sha256=digest(raw), xml_sha256=digest(xml))
            for test in required:
                expected = "FAIL" if baseline and test in PAIR else "PASS"
                assert names.get(test) == expected and ("--- " + expected + ": " + test + " ") in go, (label, test, names)
            if baseline:
                for message in ("ran before the occupied worker returned", "delivered packets = [1], want [1 2]"):
                    assert message in go, (label, message, go[-4000:])
                assert "queued packets =" not in go, "wrong number of packets in the worker queue"
                owner["early_notification"] = True
                owner["peer_delivered"] = [1]
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
        "pkg/tcpip/link/fdbased/endpoint.go", TEST, "pkg/tcpip/link/fdbased/processors.go", "runsc/gofer/net/netgofer.go"]
    assert subprocess.check_output(["bazel", "--version"], text=True).strip() == "bazel 9.3.1"
    receipt = {"source": FIXED, "base": BASE, "arch": platform.machine(), "cases": {}}
    test_source = subprocess.check_output(["git", "show", FIXED + ":" + TEST])
    receipt["test_sha256"] = digest(test_source)
    for arm, source, labels in (("before", BASE, (FD,)), ("after", FIXED, (FD,))):
        case = receipt["cases"][arm] = {"verified": False}
        try:
            git("switch", "--detach", source)
            if arm == "before":
                git("restore", "--source=" + FIXED, "--worktree", "--", TEST)
            assert Path(TEST).read_bytes() == test_source
            check_case(arm, labels, case, baseline=arm == "before")
            case["verified"] = True
        except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as exc:
            case["error"] = type(exc).__name__
            traceback.print_exc()
        finally:
            (ROOT / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
            git("restore", "--worktree", "--", TEST)
    receipt["verified"] = all(case["verified"] for case in receipt["cases"].values())
    (ROOT / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    assert receipt["verified"], "one or more source arms did not qualify"



if __name__ == "__main__":
    main()
