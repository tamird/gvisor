"""Exercise the PTY flush syscall test with and without its writable notification."""

import json
import os
from pathlib import Path
import re
import subprocess


BASE = "ccf8c9479bad5a0210c33e6e6c93f7829fbb2026"
FIX = "5dc69bf4cd9e751e18c512c90455ceb75a25a702"
CASE = "PtyTest.TCFLSHWakesReplicaPolloutAfterMasterInputFlush"
NATIVE = "pty_test_native"
SYSTRAP = "pty_test_runsc_systrap_directfs"
ROOT = Path(os.environ["RUNNER_TEMP"])
OUT = ROOT / "pty-flush"
SOURCE = Path("pkg/sentry/fsimpl/devpts/line_discipline.go")


def run(phase: str, revision: str, owners: tuple[str, ...], repeat: int) -> None:
    subprocess.run(["git", "switch", "--quiet", "--detach", revision], check=True)
    if phase == "missing-wake":
        text = SOURCE.read_text()
        wake = "case &l.outQueue:\n\t\tl.replicaWaiter.Notify(waiter.WritableEvents)"
        assert text.count(wake) == 1, wake
        SOURCE.write_text(text.replace(wake, "case &l.outQueue:\n\t\treturn"))

    targets = ["//test/syscalls:" + owner for owner in owners]
    bep = ROOT / (phase + ".bep")
    args = [
        "bazel", "--bazelrc=" + str(ROOT / "pty.bazelrc"), "test", "--config=x86_64",
        "-c", "opt", "--jobs=32", "--keep_going", "--strategy=TestRunner=remote",
        "--nocache_test_results", "--flaky_test_attempts=1",
        "--modify_execution_info=^TestRunner$=+no-remote-cache", "--remote_download_outputs=toplevel",
        r"--remote_download_regex=.*[/]test[.](log|xml)$", "--test_sharding_strategy=disabled",
        "--test_output=errors", "--test_timeout=240", "--test_env=GTEST_FILTER=" + CASE,
        f"--test_env=GTEST_REPEAT={repeat}", "--build_event_json_file=" + str(bep), *targets,
    ]
    with (ROOT / (phase + ".log")).open("w") as stream:
        result = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT)
    events = [json.loads(line) for line in bep.read_text().splitlines()]
    rows = []
    for target, owner in zip(targets, owners):
        attempts = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label") == target]
        if len(attempts) != 1:
            for line in (ROOT / (phase + ".log")).read_text().splitlines():
                if line.startswith("ERROR:") and "header" not in line:
                    print(line[:500], flush=True)
            raise AssertionError((target, attempts))
        attempt, = attempts
        test_log = (Path("bazel-testlogs/test/syscalls") / owner / "test.log").read_text()
        cases = re.findall(r"^\[\s*(OK|FAILED)\s*\] " + re.escape(CASE) + r" \(", test_log, re.MULTILINE)
        row = {
            "target": target, "revision": revision, "bazelExit": result.returncode,
            "status": attempt.get("status"),
            "cached": bool(attempt.get("cachedLocally") or attempt.get("executionInfo", {}).get("cachedRemotely")),
            "invocation": next(e["started"]["uuid"] for e in events if "started" in e),
            "cases": cases,
            "diagnostics": [l[:240] for l in test_log.splitlines() if "pty.cc:" in l or "Value of:" in l or "Actual:" in l],
        }
        rows.append(row)
        print(phase, json.dumps(row), flush=True)
        (OUT / f"{phase}-{owner}.json").write_text(json.dumps(row, indent=2) + "\n")
    for row in rows:
        assert not row["cached"], row
        assert row["cases"], row
        if phase == "missing-wake":
            assert row["status"] == "FAILED" and result.returncode == 3, row
            assert row["cases"] == ["FAILED"], row
            assert any("epoll_wait" in l for l in row["diagnostics"]), row
        elif phase == "candidate":
            assert row["status"] == "PASSED" and result.returncode == 0, row
            assert row["cases"] == ["OK"] * repeat, row
        elif row["status"] != "PASSED":
            assert row["status"] == "FAILED" and result.returncode == 3, row
            assert any("poll(" in l for l in row["diagnostics"]), row


if __name__ == "__main__":
    run("candidate", FIX, (NATIVE, SYSTRAP), 16)
    run("stock", BASE, (NATIVE,), 24)
    run("missing-wake", FIX, (SYSTRAP,), 1)

