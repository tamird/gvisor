"""Qualify actual TUN input and output on the two Linux syscall owners."""
import json
import os
from pathlib import Path
import re
import subprocess

import previous


root = Path(os.environ["RUNNER_TEMP"])
out = root / "tun-buffer-ownership"
out.mkdir()
previous.OUT = out / "dependency-inputs"
previous.OUT.mkdir()
source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert source == "b332e9477b2ff7d2d0ee06983c4f5134f061fdb0"
go = "//pkg/tcpip/link/tun:tun_test"
native = "//test/syscalls:tuntap_test_native"
systrap = "//test/syscalls:tuntap_test_runsc_systrap_directfs"
case = "TuntapTest.TruncatedReadConsumesPacket"
rows = {}
for phase, targets in (("native", [native]), ("guest-and-unit", [systrap, go])):
    bep = root / (phase + ".bep")
    log = out / (phase + ".log")
    args = [
        str(root / "tun-tools/bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
        "test", "--config=x86_64", "--jobs=4", "--lockfile_mode=off",
        "--module_mirrors=https://bcr.cloudflaremirrors.com", "--remote_executor=",
        "--remote_cache=", "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
        "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
        "--strategy=TestRunner=local", "--build_tag_filters=", "--test_tag_filters=",
        "--nocache_test_results", "--runs_per_test=1", "--flaky_test_attempts=1",
        "--test_sharding_strategy=disabled", "--test_output=errors", "--test_timeout=300",
        "--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1",
        "--test_env=GTEST_FILTER=" + case, "--build_event_json_file=" + str(bep),
    ]
    if phase == "native":
        args.append("--run_under=/usr/bin/unshare --user --map-root-user --net --")
    for attempt in range(12):
        with log.open("w") as f:
            code = subprocess.run(args + ["--distdir=" + str(d) for d in sorted(previous.DIRS)]
                                  + targets, stdout=f, stderr=subprocess.STDOUT).returncode
        if not code or not any(s in log.read_text() for s in ("Error downloading", "Error fetching", "download failed")):
            break
        if not previous.recover(log.read_text()):
            break
        log.rename(out / f"{phase}-download-{attempt}.log")
    assert code == 0, log.read_text()[-6000:]
    events = [json.loads(line) for line in bep.read_text().splitlines()]
    for target in targets:
        attempts = [e["testResult"] for e in events
                    if e.get("id", {}).get("testResult", {}).get("label") == target]
        assert len(attempts) == 1, (target, len(attempts))
        result = attempts[0]
        info = result.get("executionInfo", {})
        assert result["status"] == "PASSED" and not result.get("cachedLocally") and not info.get("cachedRemotely"), result
        assert info.get("strategy") in ("standalone", "local"), info
        directory = Path("bazel-testlogs") / target[2:].replace(":", "/")
        raw = (directory / "test.log").read_bytes()
        name = target.split(":")[1]
        (out / (name + ".log")).write_bytes(raw)
        (out / (name + ".xml")).write_bytes((directory / "test.xml").read_bytes())
        named = ["TestWriteSplitHeaders", "TestReadPacketInfo/OwnedHeadroom", "TestReadPacketInfo/SharedHeadroom"] if target == go else [case]
        for expected in named:
            pattern = (r"--- (PASS|FAIL|SKIP): " if target == go else r"\[\s*(OK|SKIPPED|FAILED)\s*\]\s+") + re.escape(expected) + r"(?=\s|$)"
            readings = re.findall(pattern, raw.decode(errors="replace"))
            assert readings and set(readings) == ({"PASS"} if target == go else {"OK"}), (target, expected, readings)
        rows[target] = {"source": source, "strategy": info["strategy"], "passed": named}
        (out / "cases.json").write_text(json.dumps(rows, indent=2) + "\n")
        print(target, json.dumps(rows[target]), flush=True)
