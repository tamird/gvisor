"""Compare both veth endpoints using the existing setlink and veth unit tests."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess

import qualification_report as report

ROOT = Path(os.environ["RUNNER_TEMP"])
PUBLIC = ROOT / "veth-public"
PRODUCT = "11004be74a5724b2cc3769eb41c9be31e699c432"
STOCK = "e8489c86088a020aba72296a60c2be1c75648fbd"
BASE = "400bd2c81dcc0349033a8218bdf38e239d256678"
UNIT = "//pkg/tcpip/link/veth:veth_test"
NATIVE = "//test/rtnetlink:setlink_test_native"
SYSTRAP = "//test/rtnetlink:setlink_test_runsc_systrap_overlay_directfs"
EXPECTED = {
    "native": [1500, 3000, 1500, 3000, 1460],
    "stock": [1486, 2986, 2986, 1446, 1446],
    "fixed": [1486, 2986, 1486, 2986, 1446],
}
FIELDS = ["initial_peer_mtu", "first_mtu", "peer_after_first", "first_after_peer", "peer_mtu"]
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
    out = ROOT / "veth-private" / name
    out.mkdir(parents=True)
    for field, args in {
        "commit_file": ("cat-file", "commit", "HEAD"),
        "source_before": ("status", "--porcelain", "--untracked-files=no"),
    }.items():
        (out / field).write_bytes(subprocess.check_output(["git", *args]))
    raw = out / "raw.jsonl"
    args = ["test", "--config=x86_64", "--keep_going", "--strip=never",
            "--lockfile_mode=off", "--test_sharding_strategy=disabled",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=180",
            "--remote_download_outputs=toplevel", "--test_output=errors",
            f"--test_env=VETH_MTU_QUALIFICATION={os.environ['GITHUB_RUN_ID']}-{name}",
            *(["--test_arg=-test.v"] if name == "unit" else []),
            f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", target]
    (out / "executed_arguments").write_text("\n".join(args) + "\n")
    command = ["docker", "exec", "bridge-gnu", "timeout", "--signal=INT", "--kill-after=30s", "1080",
               "bazel", f"--bazelrc={ROOT / 'bridge.bazelrc'}", *args]
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
    assert git("rev-parse", "HEAD") == source
    assert not (out / "source_before").read_text().strip() and not (out / "source_after").read_text().strip()
    assert info.get("strategy") == "remote" and not observation.get("cachedLocally") and not info.get("cachedRemotely")
    log_path, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.log"]
    log = log_path.read_text(errors="replace")
    if name == "unit":
        row["mtu_test_passed"] = "--- PASS: TestMTU " in log
        assert row["mtu_test_passed"] and code == 0 and observation["status"] == "PASSED"
        row["status"] = "PASS"
    else:
        readings = {}
        for field in FIELDS:
            values = re.findall(rf"\+ {field}=([0-9]+)\b", log)
            if values:
                assert len(set(values)) == 1
                readings[field] = int(values[0])
        row["mtu_readings"] = readings
        save()
        assert [readings.get(field) for field in FIELDS] == EXPECTED[name]
        if name == "stock":
            assert "[[ 2986 == 1486 ]]" in log.replace("\\", "")
            assert code == 3 and observation["status"] == "FAILED"
            row["status"] = "EXPECTED_FAILURE"
        else:
            assert code == 0 and observation["status"] == "PASSED"
            row["status"] = "PASS"
    save()


result = dict(source=os.environ["GITHUB_SHA"], product=PRODUCT, stock=STOCK, base=BASE,
              started_at=utc(), status="INCOMPLETE", arms={})
save()
try:
    assert git("rev-parse", "HEAD^") == STOCK
    assert git("rev-parse", STOCK + "^") == PRODUCT
    assert git("rev-parse", PRODUCT + "^") == BASE
    endpoint = "pkg/tcpip/link/veth/veth.go"
    assert git("diff", "--name-only", STOCK, PRODUCT) == endpoint
    assert git("rev-parse", STOCK + ":" + endpoint) == git("rev-parse", BASE + ":" + endpoint)
    assert git("diff", "--name-only", BASE, PRODUCT).splitlines() == [
        "pkg/tcpip/link/veth/BUILD", endpoint, "pkg/tcpip/link/veth/veth_test.go", "test/rtnetlink/linux/setlink_test.sh"]
    for name, source, target in (("native", STOCK, NATIVE), ("stock", STOCK, SYSTRAP),
                                 ("fixed", PRODUCT, SYSTRAP), ("unit", PRODUCT, UNIT)):
        try:
            execute(name, source, target)
        except Exception as error:
            result["arms"].setdefault(name, {"status": "INCOMPLETE"})["verifier_error"] = type(error).__name__
            save()
    statuses = {name: row["status"] for name, row in result["arms"].items()}
    result["status"] = "PASS" if statuses == {"native": "PASS", "stock": "EXPECTED_FAILURE", "fixed": "PASS", "unit": "PASS"} else (
        "FAIL" if "FAIL" in statuses.values() else "INCOMPLETE")
finally:
    save()
assert result["status"] == "PASS"
