"""Run the original packet tests and compare the persist wakeup to its backoff."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
from urllib.parse import unquote, urlsplit

PRODUCT = "9d18fb91035694d1961188202b03e33efe69b87c"
MASTER = "3847ed738e1a59892b936aebb1b9164c4f31ee95"
SENDER = Path("pkg/tcpip/transport/tcp/snd.go")
PERSIST = Path("test/packetimpact/tests/tcp_zero_window_probe_usertimeout_test.go")
ORDINARY = "tcp_user_timeout"
PROBE = "tcp_zero_window_probe"
TIMEOUT = "tcp_zero_window_probe_usertimeout"
ROOT = "//test/packetimpact/tests:"
WITNESS = re.compile(r"PERSIST_TIMING first_ms=(\d+) second_ms=(\d+) rto_us=(\d+) uto_ms=(\d+) elapsed_ms=(\d+) errno=(\d+) ready=(\d+)")
EXPECTED = {
    ORDINARY: {"TestTCPUserTimeout", "TestUserTimeoutOnNextFlight"} | {
        "TestTCPUserTimeout/" + name + kind
        for name in ("NoUserTimeout", "ACKBeforeUserTimeout", "ACKAfterUserTimeout")
        for kind in ("AfterPayload", "AfterFIN")
    } | {"TestUserTimeoutOnNextFlight/beforeNextRTO", "TestUserTimeoutOnNextFlight/afterNextRTO"},
    PROBE: {"TestZeroWindowProbe"}, TIMEOUT: {"TestZeroWindowProbeUserTimeout"},
}
TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "persist-flight"
REPORT = {"source": PRODUCT, "master": MASTER, "machine": platform.machine(), "phases": {}}


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def owner(suite, implementation):
    return f"{ROOT}{suite}_{implementation}_test"


def record(phase, event, names, expect_negative):
    label = event["id"]["testResult"]["label"]
    test = event["testResult"]
    entry = {"status": test.get("status"), "execution": test.get("executionInfo", {}).get("strategy"),
             "cached": bool(test.get("cachedLocally") or test.get("executionInfo", {}).get("cachedRemotely")),
             "cases": {}, "timing": [], "diagnostics": []}
    assert label in names and label not in phase["owners"]
    phase["owners"][label] = entry
    item, = [v for v in test.get("testActionOutput", []) if v["name"] == "test.log"]
    uri = urlsplit(item["uri"])
    assert uri.scheme == "file" and not uri.netloc
    log = Path(unquote(uri.path))
    assert log.resolve() == (Path("bazel-testlogs/test/packetimpact/tests") / label.split(":")[-1] / "test.log").resolve()
    data = log.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    assert not item.get("digest") or digest == item["digest"]
    entry["logSHA256"] = digest
    for line in data.decode(errors="replace").splitlines():
        if match := re.search(r"--- (PASS|FAIL|SKIP): (Test[A-Za-z0-9_/]+) ", line):
            if match[2] in EXPECTED[names[label]]:
                assert match[2] not in entry["cases"]
                entry["cases"][match[2]] = match[1]
        if match := WITNESS.search(line):
            entry["timing"].append(dict(zip(("firstMs", "secondMs", "rtoUs", "utoMs", "elapsedMs", "errno", "ready"), map(int, match.groups()))))
        for reason in ("do not distinguish", "expected a second zero-window probe", "want ETIMEDOUT",
                       "expected a TCP RST", "no disjoint old/new deadline", "RTO after retransmitting b",
                       "on new flight", "unexpected starting RTO", "operation not permitted"):
            if reason in line and reason not in entry["diagnostics"]:
                entry["diagnostics"].append(reason)
    expected = "FAIL" if expect_negative else "PASS"
    return (entry["status"] == ("FAILED" if expect_negative else "PASSED") and not entry["cached"]
            and entry["cases"] == dict.fromkeys(EXPECTED[names[label]], expected))


def run(name, suites, implementations, timing=False, negative=False):
    names = {owner(s, i): s for s in suites for i in implementations}
    phase = REPORT["phases"][name] = {"sender": git("hash-object", str(SENDER)), "owners": {}}
    save()
    bep = OUT / (name + ".jsonl")
    cmd = [str(TMP / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "test",
           "--config=x86_64", "--jobs=3", "--strategy=TestRunner=local", "--test_env=GO_TEST_WRAP_TESTV=1",
           "--test_sharding_strategy=disabled", "--nocache_test_results", "--flaky_test_attempts=1",
           "--runs_per_test=1", "--test_timeout=200", "--keep_going", "--remote_cache=", "--remote_executor=",
           "--build_event_json_file=" + str(bep), *names]
    with (OUT / (name + ".log")).open("w") as stream:
        phase["exit"] = subprocess.run(cmd, stdout=stream, stderr=subprocess.STDOUT).returncode
    ok = phase["exit"] == (3 if negative else 0)
    if bep.exists():
        with bep.open() as stream:
            for line in stream:
                e = json.loads(line)
                if "testResult" in e and "testResult" in e["id"]:
                    try:
                        ok = record(phase, e, names, negative) and ok
                    except (AssertionError, ValueError, KeyError, OSError) as err:
                        phase["reportError"] = type(err).__name__
                        ok = False
    ok = ok and phase["owners"].keys() == names.keys()
    if timing:
        for row in phase["owners"].values():
            if len(row["timing"]) != 1:
                ok = False
                continue
            w, = row["timing"]
            ok = ok and w["secondMs"] > w["firstMs"] > 0 and w["rtoUs"] > 0
            ok = ok and ((w["ready"] == 0 and w["errno"] == 0 and "want ETIMEDOUT" in row["diagnostics"])
                         if negative else (w["ready"] == 1 and w["errno"] == 110))
    phase["verified"] = ok
    save()


def main():
    assert platform.machine() == "x86_64" and git("rev-parse", "HEAD") == PRODUCT
    assert git("rev-parse", "HEAD^") == MASTER
    # Run the exact proposed source before adding the numerical observation.
    run("product", (ORDINARY, PROBE, TIMEOUT), ("native", "netstack"))
    original_test = PERSIST.read_text()
    first = "\tready := dut.Poll(t,"
    second = "\tif len(ready) != 1 ||"
    assert original_test.count(first) == original_test.count(second) == 1
    tapped = original_test.replace(first, "\trto := dut.GetSockOptTCPInfo(t, acceptFd).RTO\n" + first)
    PERSIST.write_text(tapped.replace(second, '''
	t.Logf("PERSIST_TIMING first_ms=%d second_ms=%d rto_us=%d uto_ms=%d elapsed_ms=%d errno=%d ready=%d", probeDelay.Milliseconds(), secondProbe.Sub(start).Milliseconds(), rto, userTimeout.Milliseconds(), time.Since(start).Milliseconds(), errno, len(ready))
''' + second))
    REPORT["instrumentedTest"] = git("hash-object", str(PERSIST))
    candidate_sender = SENDER.read_text()
    try:
        run("productTiming", (TIMEOUT,), ("native", "netstack"), timing=True)
        # Deliberately omit only the deadline adjustment on the merged
        # no-data-in-flight branch. Preserve every other timer arm.
        key = "\tdefer s.limitResendTimerToUserTimeout()"
        assert candidate_sender.count(key) == 1
        SENDER.write_text(candidate_sender.replace(key, "\tif !s.zeroWindowProbing || s.SndUna != s.SndNxt {\n\t" + key + "\n\t}"))
        run("missingEarlyProbeCap", (TIMEOUT,), ("netstack",), timing=True, negative=True)
        stock = subprocess.check_output(["git", "show", MASTER + ":" + str(SENDER)])
        SENDER.write_bytes(stock)
        run("masterTiming", (TIMEOUT,), ("netstack",), timing=True)
    finally:
        SENDER.write_text(candidate_sender)
        PERSIST.write_text(original_test)
    p = REPORT["phases"]
    assert all(v["verified"] for v in p.values())
    fixed, old = (p[key]["owners"][owner(TIMEOUT, "netstack")]["timing"][0]
                  for key in ("productTiming", "masterTiming"))
    REPORT["backedOffRTO"] = fixed["rtoUs"] > 2 * fixed["firstMs"] * 1000 and old["rtoUs"] < 2 * old["firstMs"] * 1000
    save()
    assert REPORT["backedOffRTO"]


if __name__ == "__main__":
    main()
