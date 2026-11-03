"""Read real packets from the restored idle and already-waiting TCP sockets."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess

import qualification_report as report

ROOT = Path(os.environ["RUNNER_TEMP"])
PUBLIC = ROOT / "cork-public"
PRODUCT = "2d658aad0d1c6c51846e5eda853d03768055de7e"
FIXTURE = "1a9e7e24bd13ce94c622a2daa0e66424c15dd5e7"
STOCK = "873223725113a031eecbcfd97df51b60fbe56235"
BASE = "8ec04c32dda90b8ae48b0a1bad1e991d337de5f0"
GO = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
SECRETS = [value for key in ("GH_TOKEN", "GITHUB_TOKEN", "BUILDBUDDY_API_KEY")
           if (value := os.environ.get(key))]


def utc():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(["git", *args]).decode().strip()


def save():
    result["verdict_available_at"] = utc()
    data = json.dumps(result, indent=2, sort_keys=True) + "\n"
    assert len(data.encode()) < 128 * 1024
    assert not any(value in data or json.dumps(value)[1:-1] in data for value in SECRETS)
    assert not re.search(r"(?i)(?:authorization|x-buildbuddy-api-key)\s*[:=]|github_pat_|gh[pousr]_", data)
    (PUBLIC / "verdict.json").write_text(data)


def execute(name, source, target):
    git("checkout", "--quiet", "--detach", source)
    out = ROOT / "cork-private" / name
    out.mkdir(parents=True)
    for field, args in {
        "commit_file": ("cat-file", "commit", "HEAD"),
        "source_before": ("status", "--porcelain", "--untracked-files=no"),
    }.items():
        (out / field).write_bytes(subprocess.check_output(["git", *args]))
    raw = out / "raw.jsonl"
    args = ["test", "--config=x86_64", "--keep_going", "--strip=never",
            "--lockfile_mode=off", "--test_filter=^TestTCPMigration$/^Cork(Idle|Pending)$", "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_sharding_strategy=disabled",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=180",
            "--remote_download_outputs=toplevel", "--test_output=errors",
            f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", target]
    (out / "executed_arguments").write_text("\n".join(args) + "\n")
    command = ["docker", "exec", "cork-gnu", "timeout", "--signal=INT", "--kill-after=30s", "1800",
               "bazel", f"--bazelrc={ROOT / 'cork.bazelrc'}", *args]
    with (out / "console").open("wb") as stream:
        code = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT).returncode
    finished = utc()
    row = dict(source=source, target=target, native_exit=code, command_finished_at=finished,
               status="FAIL" if code == 3 else "INCOMPLETE")
    result["arms"][name] = row
    save()
    (out / "native_exit").write_text(str(code))
    (out / "source_after").write_text(git("status", "--porcelain", "--untracked-files=no"))
    (out / "final_head").write_text(git("rev-parse", "HEAD"))
    (out / "targets").write_text(target + "\n")
    events = out / "events.jsonl"
    captured = report.capture(raw, events, Path("bazel-out"), out / "reports")
    (out / "capture_exit").write_text("1" if captured["errors"] else "0")
    context = {key: key for key in ("commit_file", "source_before", "source_after", "final_head",
                                   "executed_arguments", "native_exit", "capture_exit")}
    context.update(expected_source=source, command_kind="test", execution_finished_at=finished)
    report.write_json(out / "context.json", context)
    verdict = report.verdict(events, report.manifest_path(events), out / "context.json", out / "targets", ())
    row.update(original_status=verdict["status"], case_counts=verdict["case_counts"],
               source_clean=not verdict["errors"], evidence_complete=not (verdict["errors"] or verdict["missing"]),
               reporting_elapsed_seconds=verdict["reporting_elapsed_seconds"],
               execution_to_verdict_seconds=verdict["execution_to_verdict_seconds"])
    records = [json.loads(line) for line in events.read_text().splitlines()]
    starts = [item["started"]["uuid"] for item in records if "started" in item]
    assert len(starts) == 1 and re.fullmatch(r"[a-f0-9-]{36}", starts[0])
    row["invocation"] = starts[0]
    row["reports"] = [{key: item[key] for key in ("name", "sha256", "bytes")} for item in captured["reports"]]
    attempts = [item for item in records if "testResult" in item]
    assert len(attempts) == 1 and len(verdict["executed"]) == 1
    attempt = attempts[0]
    identity, observation = attempt["id"]["testResult"], attempt["testResult"]
    assert identity["label"] == target and re.fullmatch(r"[a-f0-9]{64}", identity["configuration"]["id"])
    assert all(identity.get(key, 1) == 1 for key in ("run", "shard", "attempt"))
    info = observation["executionInfo"]
    row["attempt"] = dict(configuration=identity["configuration"]["id"],
                          strategy=info.get("strategy"), cached_locally=observation.get("cachedLocally", False),
                          cached_remotely=info.get("cachedRemotely", False), status=observation["status"])
    row["test_timing"] = {key: observation[key] for key in (
        "testAttemptStart", "testAttemptStartMillis", "testAttemptDuration", "testAttemptDurationMillis") if key in observation}
    save()
    assert row["evidence_complete"] and info.get("strategy") == "remote"
    assert not observation.get("cachedLocally") and not info.get("cachedRemotely")
    log, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.log"]
    log_text = log.read_text(errors="replace")
    leaves = {case: re.findall(r"--- (PASS|FAIL|SKIP): TestTCPMigration/Cork" + case + r" ", log_text)
              for case in ("Idle", "Pending")}
    early = 'restored cork released "z" before the write deadline' in log_text
    row.update(leaves=leaves, idle_early_payload_observed=early)
    save()
    assert not re.search(r"--- SKIP:|panic:", log_text)
    if name == "old":
        assert leaves == {"Idle": ["FAIL"], "Pending": ["PASS"]} and early
        assert code == 3 and observation["status"] == "FAILED" and verdict["status"] == "FAIL"
        row["status"] = "EXPECTED_FAILURE"
    else:
        assert leaves == {"Idle": ["PASS"], "Pending": ["PASS"]} and not early
        assert code == 0 and observation["status"] == "PASSED" and verdict["status"] == "PASS"
        row["status"] = "PASS"
    save()


result = dict(source=os.environ["GITHUB_SHA"], product=PRODUCT, stock=STOCK, base=BASE,
              started_at=utc(), status="INCOMPLETE", arms={})
save()
try:
    assert git("rev-parse", "HEAD^") == STOCK
    assert git("rev-parse", STOCK + "^") == FIXTURE
    assert git("rev-parse", FIXTURE + "^") == PRODUCT
    assert git("rev-parse", PRODUCT + "^") == BASE
    files = ["pkg/tcpip/transport/tcp/endpoint_state.go", "pkg/tcpip/transport/tcp/snd.go"]
    assert git("diff", "--name-only", PRODUCT, BASE).splitlines() == files
    assert git("diff", "--name-only", FIXTURE, STOCK).splitlines() == files
    for f in files:
        assert git("rev-parse", STOCK + ":" + f) == git("rev-parse", BASE + ":" + f)
    assert git("diff", "--name-only", PRODUCT, FIXTURE) == "pkg/tcpip/transport/tcp/test/e2e/tcp_migration_test.go"
    for name, source in (("old", STOCK), ("fixed", FIXTURE)):
        try:
            execute(name, source, GO)
        except Exception as error:
            result["arms"].setdefault(name, {"status": "INCOMPLETE"})["verifier_error"] = type(error).__name__
            save()
    statuses = {name: row["status"] for name, row in result["arms"].items()}
    result["status"] = "PASS" if statuses == {"old": "EXPECTED_FAILURE", "fixed": "PASS"} else (
        "FAIL" if "FAIL" in statuses.values() else "INCOMPLETE")
finally:
    save()
assert result["status"] == "PASS"
