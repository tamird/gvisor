"""Qualify the credited Eifel cancellation with the existing packet owner."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

import qualification_report as report

OWNER = "//pkg/tcpip/transport/tcp/test/e2e:tcp_sack_test"
CASE = "TestEifelReplayCancellation"
LEAVES = ("original_ack", "original_ack_then_real_loss", "repair_ack")
SOURCES = {"candidate": "45868c5470d96319f7a56696247a09b2986aedec", "without_cancellation": "b9b05337b03754c2e75b72355610c23478d61ae3"}
BLOBS = {
    "pkg/tcpip/tcpip.go": "d2c0393bc8201dfe94e1030e39d971eacdf1ee3d",
    "pkg/tcpip/transport/tcp/cubic.go": "812f2556a2753c6b3f2245a8c99b66ee2b1f8bc4",
    "pkg/tcpip/transport/tcp/rack.go": "17d86b09391c99ed027d707cf125ac7227979dbf",
    "pkg/tcpip/transport/tcp/snd.go": "4390b612e6190d4a6dedae892c821036e58210fe",
    "pkg/tcpip/transport/tcp/undo.go": "b4eb4d37381c2ceee2831f71e88275cb34e8a62b",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def git(*args: str) -> bytes:
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    assert Path.cwd() == workspace, "exact coordinator checkout"
    return subprocess.check_output(["git", "-c", "safe.directory=" + str(workspace), *args])


def write_json(path: Path, value: object) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    # No raw console, headers, arbitrary XML names or command arguments enter
    # the public record. Also reject the credential from the private RC.
    rc_text = Path(os.environ["BAZEL_RC"]).read_text()
    for secret in re.findall(r"x-buildbuddy-api-key=([^\s]+)", rc_text):
        assert secret not in text and json.dumps(secret)[1:-1] not in text
    assert not re.search(r"(?i)x-buildbuddy-api-key|authorization\s*[:=]|bearer\s+", text)
    assert len(text.encode()) < 256 * 1024
    path.write_text(text)
    path.chmod(0o644)


def run(arm: str, destination: Path) -> dict[str, object]:
    source = SOURCES[arm]
    negative = arm == "without_cancellation"
    d = destination / arm
    d.mkdir()
    d.chmod(0o755)
    r: dict[str, object] = {"arm": arm, "source": source, "owner": OWNER, "status": "INCOMPLETE"}
    code = None
    launcher = Path("tools/bazeldefs/go.bzl")
    original_launcher = launcher.read_bytes()
    try:
        assert git("rev-parse", "HEAD").decode().strip() == source
        assert not git("status", "--porcelain", "--untracked-files=no")
        source_blobs = {p: git("rev-parse", f"HEAD:{p}").decode().strip() for p in BLOBS}
        r["source_blobs"] = source_blobs
        for p, expected in BLOBS.items():
            assert source_blobs[p] == ("9abf6720a036f87c9417c4ee0b6a051bdb891db4" if negative and p.endswith("/undo.go") else expected), p
        r["fixture_blob"] = git("rev-parse", "HEAD:pkg/tcpip/transport/tcp/test/e2e/tcp_sack_test.go").decode().strip()
        assert r["fixture_blob"] == "eae3d8730b2eb62fc82549ef84675ef7002cf8b8"
        # The existing Eifel harness's identical bootstrap correction supplies
        # the interpreter for the old generated shell launcher on Linux RBE.
        old = b'content = "PATH=$PWD/{} exec {} {} > {}".format('
        assert original_launcher.count(old) == 1
        launcher.write_bytes(original_launcher.replace(old, b'content = "#!/bin/sh\\nPATH=$PWD/{} exec {} {} > {}".format('))
        r["launcher_blob"] = git("hash-object", str(launcher)).decode().strip()
        assert git("diff", "--name-only").decode().splitlines() == [str(launcher)]
        invocation = str(uuid.uuid4())
        r["invocation"] = invocation
        bazel = [os.environ["BAZEL"], "--bazelrc=" + os.environ["BAZEL_RC"]]
        args = ["test", "--config=x86_64", "--invocation_id=" + invocation, "--jobs=16",
                "--strategy=TestRunner=remote", "--nocache_test_results", "--runs_per_test=1",
                "--flaky_test_attempts=1", "--test_sharding_strategy=disabled", "--test_output=errors",
                "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v", "--test_timeout=120",
                "--test_filter=^" + CASE + "$", "--build_event_json_file=" + str(d/"events.raw"),
                "--build_event_json_file_path_conversion=false",
                "--remote_download_regex=.*[/](test[.](log|xml)|test[.]outputs/.*)$", OWNER]
        with (d/"command.log").open("wb") as stream:
            code = subprocess.run(["timeout", "-k", "15", "2100", *bazel, *args], stdout=stream, stderr=subprocess.STDOUT).returncode
        r.update(native_exit=code, command_finished_at=now())
        write_json(d/"result.json", r)
        log = (d/"command.log").read_text(errors="replace")
        r["console_sha256"] = hashlib.sha256((d/"command.log").read_bytes()).hexdigest()
        r["diagnostic_classes"] = [name for name in ("Permission denied", "undefined:", "compilation failed", "RESOURCE_EXHAUSTED", "no such package", "no such target", "missing return") if name in log]
        # Preserve original command outcome before trying to collect reports.
        outroot = Path(subprocess.check_output(bazel+["info", "output_base"], stderr=subprocess.DEVNULL, text=True).strip())
        clean = d/"events.jsonl"
        manifest = report.capture(d/"events.raw", clean, outroot, d/"reports")
        r["capture_errors"] = len(manifest["errors"])
        events = [json.loads(line) for line in clean.read_text().splitlines()]
        started = [e["started"]["uuid"] for e in events if "started" in e]
        assert started == [invocation], started
        attempts = [e for e in events if "testResult" in e]
        recorded_attempts = [{"identity": e["id"]["testResult"], "status": e["testResult"]["status"],
                          "remote": e["testResult"].get("executionInfo", {}).get("strategy") == "remote",
                          "cached": bool(e["testResult"].get("cachedLocally") or e["testResult"].get("executionInfo", {}).get("cachedRemotely")),
                          "start": e["testResult"].get("testAttemptStart"), "duration": e["testResult"].get("testAttemptDuration"),
                          "start_millis": e["testResult"].get("testAttemptStartMillis"), "duration_millis": e["testResult"].get("testAttemptDurationMillis")}
                         for e in attempts]
        r["build_finished"] = [{k:e["finished"].get(k) for k in ("finishTime", "finishTimeMillis", "overallSuccess")} for e in events if "finished" in e]
        r["attempts"] = recorded_attempts
        assert len(attempts) == 1, "one expected owner attempt"
        attempt = recorded_attempts[0]
        assert attempt["identity"]["label"] == OWNER and attempt["remote"] and not attempt["cached"], "fresh remote owner"
        r["report_files"] = [{k: f[k] for k in ("identity", "name", "bytes", "sha256")} for f in manifest["reports"]]
        xmls = [d/f["path"] for f in manifest["reports"] if f["name"] == "test.xml"]
        logs = [d/f["path"] for f in manifest["reports"] if f["name"] == "test.log"]
        assert len(xmls) == len(logs) == 1, "original XML and log required"
        expected_names = {CASE, *(CASE + "/" + leaf for leaf in LEAVES)}
        cases = []
        diagnostic_cases = []
        for case in ET.parse(xmls[0]).iter("testcase"):
            name = case.get("name")
            assert name in expected_names, "unexpected XML case"
            result = "skipped" if case.get("status") == "notrun" or case.get("result") == "suppressed" else next((c.tag for c in case if c.tag in ("skipped", "failure", "error")), "pass")
            cases.append({"name": name, "result": result})
            if any("unexpected immediate replay at offset 10 after original ACK" in (child.text or "") for child in case if child.tag == "failure"):
                diagnostic_cases.append(name)
        r["cases"] = cases
        assert len(cases) == 4 and {c["name"] for c in cases} == expected_names, "exact four XML records"
        text = logs[0].read_text(errors="replace")
        pattern = re.compile(r"EIFEL_CANCEL case=(original_ack|original_ack_then_real_loss|repair_ack) original=(\d+) repair=(\d+) echo=(\d+) timeouts=(\d+) undo=(\d+) partial_outstanding=(\d+) final_outstanding=(\d+) cwnd=(\d+)")
        observations = {}
        for name, *values in pattern.findall(text):
            assert name not in observations, "duplicate packet observation"
            observations[name] = dict(zip(("original", "repair", "echo", "timeouts", "undo", "partial_outstanding", "final_outstanding", "cwnd"), map(int, values), strict=True))
        r["packet_observations"] = observations
        r["immediate_replay_failures"] = sorted(diagnostic_cases)
        assert not manifest["errors"], "report capture complete"
        if negative:
            expected_failures = {CASE, CASE + "/original_ack", CASE + "/original_ack_then_real_loss"}
            assert code == 3 and attempt["status"] == "FAILED", "negative is a real test failure"
            assert all(c["result"] == ("failure" if c["name"] in expected_failures else "pass") for c in cases), "negative case dispositions"
            assert set(diagnostic_cases) >= expected_failures - {CASE}, "both original ACKs failed specifically on replay"
            assert set(observations) == {"repair_ack"}, "repair ACK still passes"
            r["status"] = "EXPECTED_FAILURE"
        else:
            assert code == 0 and attempt["status"] == "PASSED" and all(c["result"] == "pass" for c in cases), "all actual packet cases pass"
            assert set(observations) == set(LEAVES), "all packet observations"
            for name, row in observations.items():
                assert row["original"] == 1000 and row["repair"] > 1000 and row["final_outstanding"] == 0
                assert row["undo"] == int(name != "repair_ack")
                assert row["timeouts"] == (2 if name == "original_ack_then_real_loss" else 1)
                if name != "repair_ack":
                    assert row["partial_outstanding"] == 4
            r["status"] = "PASS"
    except (AssertionError, KeyError, OSError, ValueError, ET.ParseError, subprocess.CalledProcessError) as exc:
        r["collector_error"] = type(exc).__name__
        if code in (1, 3):
            r["status"] = "FAIL"
    finally:
        launcher.write_bytes(original_launcher)
        r["verified_at"] = now()
        write_json(d/"result.json", r)
    return r


if __name__ == "__main__":
    arm = sys.argv[1]
    assert arm in SOURCES
    result = run(arm, Path(os.environ["RUNNER_TEMP"])/"records")
    sys.exit(int(result["status"] not in ("PASS", "EXPECTED_FAILURE")))
