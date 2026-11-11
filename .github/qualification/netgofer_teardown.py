"""Compare the same connection-limit test on the existing queue-drain change."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

import previous

SOURCE = "d5f35d45b22ccea5f6309726c48c9e6bdb2c45a2"
TARGET = "//runsc/gofer/net:netgofer_test"
TEST = "TestProxyLimitsConnections"
WORK = Path(os.environ["RUNNER_TEMP"]) / "netgofer-teardown"
OUT = WORK / "receipt"
TEST_SOURCE = Path("runsc/gofer/net/netgofer_test.go")
NETGOFER = Path("runsc/gofer/net/netgofer.go")
ENDPOINT = Path("pkg/tcpip/link/fdbased/endpoint.go")
DISPATCH = Path("pkg/tcpip/link/fdbased/packet_dispatchers.go")
RAW = Path("pkg/rawfile/rawfile_unsafe.go")
FIX = "fbad38ea661236e230a355fde5d0f87d9cac44bb"
REPORT = {"source": SOURCE, "architecture": platform.machine(), "arms": {}, "verified": False}


def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def replace(path, old, new):
    text = path.read_text()
    assert text.count(old) == 1, (path, old)
    path.write_text(text.replace(old, new))


def stock_observer():
    replace(NETGOFER, "type Netgofer struct {", "type Netgofer struct {\n\tobserveReceive func(int, int)")
    replace(NETGOFER, "PacketDispatchMode: fdbased.RecvMMsg,", "PacketDispatchMode: fdbased.RecvMMsg,\n\t\tObserveReceive: n.observeReceive,")
    replace(ENDPOINT, "type Options struct {", "type Options struct {\n\tObserveReceive func(int, int)")
    replace(DISPATCH, "type recvMMsgDispatcher struct {", 'type recvMMsgDispatcher struct {\n\tobserveReceive func(int, int) `state:"nosave"`')
    replace(DISPATCH, "d := &recvMMsgDispatcher{", "d := &recvMMsgDispatcher{\n\t\tobserveReceive: opts.ObserveReceive,")
    marker = "// Process each of received packets."
    replace(DISPATCH, marker, """if d.observeReceive != nil {
        bytes := 0
        for _, msg := range d.msgHdrs[:nMsgs] {
            bytes += int(msg.Len)
        }
        d.observeReceive(nMsgs, bytes)
    }

    """ + marker)
    TEST_SOURCE.write_bytes((WORK / "diagnostic.go").read_bytes())


def restore(*paths):
    for path in paths:
        path.write_bytes(subprocess.check_output(["git", "show", f"{SOURCE}:{path}"]))


def install_receive_change():
    # Only the receive path is under test in this in-process fixture. It has
    # always run without installing the production process's seccomp filter.
    changes = subprocess.check_output(["git", "diff", "--unified=1", FIX + "^", FIX, "--", RAW, ENDPOINT, DISPATCH])
    subprocess.run(["git", "apply", "-C1"], input=changes, check=True)
    option = "PacketDispatchMode: fdbased.RecvMMsg,"
    replace(NETGOFER, option, option + "\n\t\tDetectSeqpacketEOF: true,")
    REPORT["receiveChange"] = {"commit": FIX, "rawAndFDBasedDiffSHA256": hashlib.sha256(changes).hexdigest(),
                               "files": [str(p) for p in [RAW, ENDPOINT, DISPATCH, NETGOFER]]}


def run(name: str, count: int):
    bep = WORK / (name + ".bep")
    console = WORK / (name + ".console")
    command = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "test",
               "--config=aarch64", "--jobs=4", "--lockfile_mode=off", "--remote_executor=", "--remote_cache=",
               "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
               "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
               "--strategy=TestRunner=local", "--test_sharding_strategy=disabled", "--nocache_test_results",
               "--runs_per_test=1", "--flaky_test_attempts=1", "--build_tag_filters=", "--test_tag_filters=",
               "--test_timeout=420", "--test_output=errors", "--nobuild_event_json_file_path_conversion",
               "--test_arg=-test.v", f"--test_arg=-test.count={count}", "--test_arg=-test.failfast",
               "--test_arg=-test.run=^" + TEST + "$", "--build_event_json_file=" + str(bep)]
    for attempt in range(10):
        with console.open("w") as f:
            proc = subprocess.run([*command, *["--distdir=" + str(d) for d in sorted(previous.DIRS)], TARGET],
                                  stdout=f, stderr=subprocess.STDOUT, timeout=70*60)
        events = [json.loads(line) for line in bep.read_bytes().splitlines()] if bep.exists() else []
        results = [e for e in events if "testResult" in e and e["id"]["testResult"]["label"] == TARGET]
        if results:
            break
        log = console.read_text(errors="replace")
        if not previous.recover(log):
            REPORT["stoppedBeforeTest"] = {"phase": name, "bazelExit": proc.returncode,
                                          "errors": [line[:450] for line in log.splitlines() if "ERROR:" in line][:4]}
            save()
            raise RuntimeError(name + " stopped before the test action")
    item, = results
    result = item["testResult"]
    info = result["executionInfo"]
    assert not result.get("cachedLocally") and not info.get("cachedRemotely")
    assert info["strategy"] in {"standalone", "local"}
    outputs = {}
    for key in ["test.log", "test.xml"]:
        ref, = [f for f in result["testActionOutput"] if f.get("name") == key]
        url = urlsplit(ref["uri"])
        assert url.scheme == "file" and not url.netloc
        data = Path(unquote(url.path)).read_bytes()
        assert not ref.get("digest") or hashlib.sha256(data).hexdigest() == ref["digest"]
        outputs[key] = data
    log = outputs["test.log"].decode(errors="replace")
    cases = list(ET.fromstring(outputs["test.xml"]).iter("testcase"))
    assert any(c.get("name") == TEST and c.find("skipped") is None for c in cases), name
    # Keep the original per-case verdict and sanitized function/line evidence.
    # Transient public package-fetch output and raw runtime arguments stay on the runner.
    frames = [l.strip() for l in log.splitlines() if any(x in l for x in ["TEARDOWN_FRAME ", "TEARDOWN_STACK ", "TEARDOWN_RECEIVES "])]
    drained = re.findall(r"TEARDOWN_DRAIN\s+(-?\d+)", log)
    verdicts = re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): " + TEST + r" \(([^)]+)\)", log)
    stalled = log.count("runProxyMode() did not exit after UDS peer closed")
    row = {"planned": count, "verdicts": verdicts, "teardownTimeouts": stalled, "strategy": info["strategy"],
           "bazelExit": proc.returncode, "bazelStatus": result["status"], "frames": frames,
           "queueChecks": len(drained), "emptyQueueChecks": drained.count("0"),
           "originalLogSHA256": hashlib.sha256(outputs["test.log"]).hexdigest(),
           "started": next(e["started"]["uuid"] for e in events if "started" in e)}
    REPORT["arms"][name] = row
    save()
    assert verdicts and all(v in {"PASS", "FAIL"} for v, _ in verdicts), row
    if failures := sum(v == "FAIL" for v, _ in verdicts):
        assert failures == stalled and proc.returncode and result["status"] == "FAILED", row
    else:
        assert len(verdicts) == count and proc.returncode == 0 and result["status"] == "PASSED", row
    if name == "stock-observed" and stalled:
        assert any("(*Netgofer).runProxyMode" in f and "netgofer.go:" in f for f in frames), row
        assert any("TEARDOWN_RECEIVES " in f for f in frames), row
    if name.startswith("queue-drain"):
        assert not stalled, row
    print(name, len(verdicts), "executed,", stalled, "teardown timeouts", flush=True)


assert REPORT["architecture"] == "aarch64"
previous.save = lambda: None
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
save()
stock_observer()
run("stock-observed", 256)
restore(NETGOFER, ENDPOINT, DISPATCH, TEST_SOURCE)
install_receive_change()
corrected = DISPATCH.read_bytes()
marker = "d.queuedAtShutdown = n"
replace(DISPATCH, marker, 'println("TEARDOWN_DRAIN", n)\n\t' + marker)
run("queue-drain-observed", 128)
DISPATCH.write_bytes(corrected)
run("queue-drain-unobserved", 256)
REPORT["verified"] = True
save()
