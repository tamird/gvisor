"""Run the wire and Linux controls before replaying the reported transfer."""

import json
import os
from pathlib import Path
import re
import subprocess

BASE = "f47e3a56425482ba7090735302d6454757086d51"
FIX = "a8286c8056a6c2302f62c6dd52e9d818b9c224d9"
SND = "pkg/tcpip/transport/tcp/snd.go"
TOP = "//pkg/tcpip/transport/tcp"
REPLAY = "//test/tcp_recovery_replay:heavy_loss"
TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "rto-diagnostics"
OUT.mkdir(exist_ok=True)
report = {"baseline": BASE, "product": FIX, "reporter": "f72f18f2f7fe0e5ea4151b51ce3f67d68b3b05e2", "cases": {}}


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def save():
    (OUT / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")


def run(name, rev, target, tests, *, isolated=False):
    git("restore", "--source=" + rev, "--worktree", "--", SND)
    blob = git("hash-object", SND)
    assert blob == git("rev-parse", rev + ":" + SND)
    command = [str(TMP / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g",
               "test", "--config=x86_64", "--jobs=3", "--test_output=all", "--color=no",
               "--nocache_test_results", "--strategy=TestRunner=local", "--test_timeout=860",
               "--test_arg=-test.v", "--test_arg=-test.run=^(" + "|".join(tests) + ")$"]
    if isolated:
        command += ["--test_env=TCP_REPLAY_ARM=fixed",
                    "--test_env=TCP_RETRY_OUTER_NETNS=" + os.readlink("/proc/self/ns/net"),
                    "--run_under=/usr/bin/unshare --user --map-root-user --net --"]
    p = subprocess.Popen([*command, target], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    r = {"owner": target, "snd": blob, "tests": {}, "samples": [], "diagnostics": []}
    report["cases"][name] = r
    save()
    for raw in p.stdout:
        line = raw.strip()
        match = re.match(r"^--- (PASS|FAIL|SKIP): ([^ ]+)", line)
        if match and match[2] in tests:
            r["tests"][match[2]] = match[1]
        for prefix in ("TCP_SAMPLE arm=fixed ", "TCP_LINUX_RETRIES2=1 "):
            if line.startswith(prefix):
                r["samples"].append(json.loads(line[len(prefix):]))
                save()
        if re.search(r"\.go:\d+:|error:|panic:|^FAIL|timed out|undefined:|failed to", line, re.I) and len(r["diagnostics"]) < 45:
            r["diagnostics"].append(line[:500])
    r["exit"] = p.wait()
    if name == "old-wire":
        r["passed"] = r["exit"] != 0 and r["tests"] == {tests[0]: "FAIL"} and any("first data timeout closed the connection" in d for d in r["diagnostics"])
    else:
        r["passed"] = r["exit"] == 0 and r["tests"] == dict.fromkeys(tests, "PASS")
    if name == "heavy-fixed":
        rows = r["samples"]
        r["passed"] = r["passed"] and len(rows) == 7 and [s["seed"] for s in rows] == [1,3,4,6,1,9,12] and all(s["client_read"] == 2800000 and not s["client_error"] and s["missing_wire_bytes"] == 0 for s in rows)
    if name == "linux":
        r["passed"] = r["passed"] and len(r["samples"]) == 1
    save()
    print(name, r["passed"], r["tests"], "samples", len(r["samples"]), r["diagnostics"][:6], flush=True)


assert git("rev-parse", "HEAD^") == FIX and git("rev-parse", "HEAD^^") == BASE
for n in ("pkg/tcpip/transport/tcp/timer_test.go", "pkg/tcpip/transport/tcp/test/e2e/tcp_test.go"):
    assert git("hash-object", n) == git("rev-parse", FIX + ":" + n)
wire = "TestTailProbeDoesNotConsumeTimeoutRetry"
run("old-wire", BASE, TOP + "/test/e2e:tcp_test", [wire])
run("fixed-wire", FIX, TOP + "/test/e2e:tcp_test", [wire, "TestMaxRetransmitsTimeout", "TestTCPUserTimeout", "TestMaxRTO"])
run("bounds", FIX, TOP + ":tcp_test", ["TestRetryTimeoutBudget"])
run("linux", FIX, REPLAY, ["TestLinuxRetry"], isolated=True)
run("heavy-fixed", FIX, REPLAY, ["TestMeasure", "TestCoverageSplit"], isolated=True)
raise SystemExit(not all(r["passed"] for r in report["cases"].values()))
