"""Keep each original test result, including precondition and build failures."""

import json
import os
from pathlib import Path
import re
import subprocess

TMP = Path(os.environ["RUNNER_TEMP"])
dest = TMP / "two-flights" / "result.json"
report = {"fixture": "928962c922dc41744d9920c2abd9e894c3695d32", "product": "a8286c8056a6c2302f62c6dd52e9d818b9c224d9", "prior": "f47e3a56425482ba7090735302d6454757086d51", "runs": {}}
path = "pkg/tcpip/transport/tcp/snd.go"
fixture = "test/packetimpact/tests/tcp_user_timeout_test.go"


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def save():
    dest.write_text(json.dumps(report, indent=2) + "\n")


def run(arm, variant, source):
    git("restore", "--source=" + source, "--worktree", "--", path)
    owner = "//test/packetimpact/tests:tcp_user_timeout_" + variant + "_test"
    blob = git("hash-object", path)
    assert blob == git("rev-parse", source + ":" + path)
    result = {"source": source, "snd_blob": blob, "owner": owner, "events": [], "test": {}, "diagnostics": []}
    report["runs"][arm] = result
    save()
    log = dest.parent / (arm + ".log")
    with log.open("w") as out:
        result["exit"] = subprocess.run(
            [str(TMP / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "test",
             "--config=x86_64", "--jobs=3", "--strategy=TestRunner=local", "--test_env=GO_TEST_WRAP_TESTV=1",
             "--test_filter=^TestUserTimeoutOnNextFlight$", "--test_sharding_strategy=disabled",
             "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=200", "--test_output=all", owner],
            stdout=out, stderr=subprocess.STDOUT).returncode
    for line in log.open():
        line = line.strip()
        wire = re.search(r"TCP_FLIGHT_WIRE letter=([a-dw]) offset=(\d+) elapsed_us=(\d+)", line)
        if wire:
            result["events"].append([wire[1], int(wire[2]), int(wire[3])])
        obs = re.search(r"TCP_FLIGHT (rto_us=\d+ uto_us=\d+ rto1_us=\d+ rto2_us=\d+ fresh_us=\d+ probe_us=\d+ errno=\d+)", line)
        if obs:
            result["observation"] = {k: int(v) for k, v in (pair.split("=") for pair in obs[1].split())}
        verdict = re.search(r"--- (PASS|FAIL|SKIP): (TestUserTimeoutOnNextFlight) ", line)
        if verdict:
            result["test"][verdict[2]] = verdict[1]
        if re.search(r"ERROR:|error:|FATAL|fatal:|permission denied|undefined:|\.go:\d+|no space|failed to|exec [^:]+:", line, re.I) and len(result["diagnostics"]) < 40:
            result["diagnostics"].append(line[:420])
    save()
    print(arm, result, flush=True)


assert git("rev-parse", "HEAD^") == report["fixture"]
assert git("rev-parse", "HEAD^^") == report["product"]
assert git("rev-parse", "HEAD^^^") == report["prior"]
report["fixture_blob"] = git("hash-object", fixture)
assert report["fixture_blob"] == git("rev-parse", report["fixture"] + ":" + fixture)
run("linux", "native", report["product"])
run("corrected", "netstack", report["product"])
if report["runs"]["corrected"]["events"]:
    run("prior", "netstack", report["prior"])
verified = all(report["runs"].get(k, {}).get("test") for k in ("linux", "corrected", "prior"))
report["reached_all"] = verified
save()
raise SystemExit(not verified)
