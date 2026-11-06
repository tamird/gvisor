"""Keep each original test result, including precondition and build failures."""

import json
import os
from pathlib import Path
import re
import subprocess

TMP = Path(os.environ["RUNNER_TEMP"])
dest = TMP / "two-flights" / "result.json"
sources = {
    "linux": "f51a205a8f4afbc5ab41aded070df74951b081ea",
    "master": "f51a205a8f4afbc5ab41aded070df74951b081ea",
    "pr": "e1e5a93215f507fe735b868493a14868180f3bec",
    "deadline": "cc572743e815dc6c99dabc4f9fb5a052d8be4851",
    "combined": "2738f2eb325890e97cc87c42f2969d4c4f119d83",
}
report = {"fixture": "928962c922dc41744d9920c2abd9e894c3695d32", "runs": {}}
path = "pkg/tcpip/transport/tcp/snd.go"
fixture = "test/packetimpact/tests/tcp_user_timeout_test.go"


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def save():
    dest.write_text(json.dumps(report, indent=2) + "\n")


def run(arm, variant, source):
    git("checkout", "--quiet", "--detach", source)
    git("restore", "--source=" + report["fixture"], "--worktree", "--", fixture)
    assert git("hash-object", fixture) == report["fixture_blob"]
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
        if "SO_ERROR=110" in line:
            result["diagnostics"].append("ETIMEDOUT")
    git("restore", "--source=HEAD", "--worktree", "--", fixture)
    save()
    print(arm, result, flush=True)


report["fixture_blob"] = git("rev-parse", report["fixture"] + ":" + fixture)
for arm, src in sources.items():
    run(arm, "native" if arm == "linux" else "netstack", src)
    row = report["runs"][arm]
    obs = row.get("observation", {})
    should_live = arm in ("linux", "deadline", "combined")
    wire = [(ch, offset) for ch, offset, _ in row["events"]]
    row["flight_verified"] = (
        wire[:6] == [("w", 0), ("a", 0), ("b", 512), ("a", 0), ("a", 0), ("c", 1024)]
        and (("d", 1536) in wire) == should_live
        and obs.get("errno") == (0 if should_live else 110)
        and 0 < obs["probe_us"] - obs["fresh_us"] < obs["uto_us"] < obs["probe_us"]
        and row["exit"] == (0 if should_live else 3)
        and row["test"].get("TestUserTimeoutOnNextFlight") == ("PASS" if should_live else "FAIL")
    )
    save()

e2e = ["TestRetryLimitCountsOnlyTimeouts", "TestRetryLimitRestartsAfterProgress"]
with (dest.parent / "e2e.log").open("w") as log:
    rc = subprocess.run([str(TMP / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
        "test", "--config=x86_64", "--jobs=3", "--strategy=TestRunner=local", "--nocache_test_results",
        "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v", "--test_filter=^(" + "|".join(e2e) + ")$",
        "--test_output=all", "//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test"], stdout=log, stderr=subprocess.STDOUT).returncode
text = (dest.parent / "e2e.log").read_text(errors="replace")
report["peer_tests_on_combined"] = {name: bool(re.search(r"--- PASS: " + name + r"\s", text)) for name in e2e}
report["verified"] = rc == 0 and all(report["peer_tests_on_combined"].values()) and all(
    row["flight_verified"] for row in report["runs"].values())
save()
raise SystemExit(not report["verified"])
