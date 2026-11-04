"""Check cork timeouts and flushing on Linux and both gVisor senders."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import xml.etree.ElementTree as ET
import zipfile

import qualification_report as report

ROOT = Path(os.environ["RUNNER_TEMP"])
PUBLIC = ROOT / "cork-public"
PRODUCT = "8ec04c32dda90b8ae48b0a1bad1e991d337de5f0"
STOCK = "f69769dd4a4556762a6391e89f3133cfd6bffc05"
BASE = "c336db121f61914f6f2a761dc005bf4293f67e09"
ORIGINAL = "d1928120d01a8169846ac59f340fab5ff178424a"
SUITE = "AllTCPSockets/TCPSocketPairTest"
RECURRING = "WaitTillTimeoutWithCorkOption"
CASES = (RECURRING, "TCPCork", "TCPCorkFlushOnClose", "TCPCorkFlushOnShutdown")
IPV6 = {0, 2, 3, 5, 6, 8, 9, 11}
EXPECTED = {f"{SUITE}.{case}/{i}" for case in CASES for i in IPV6}
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


def reuse_original():
    path = ROOT / "cork-original.zip"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == "f95b964af9be2a57306eecc8db3b589d9bbd8df04eea7005d168df8e77f11e6c"
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == ["verdict.json"]
        recovered = json.loads(archive.read("verdict.json"))
    assert recovered["source"] == "aa43dcfe737f0163db81455fde68af405ca6ef0f"
    old = recovered["original"]
    assert (recovered["status"], old["status"], old["product"], old["stock"]) == (
        "ORIGINAL_REPORTS_READ", "FAIL", PRODUCT, STOCK)
    assert old["source"] == "4a3e55b3d217d699873b479945c8367183cee1e0"
    assert old["original"]["source"] == ORIGINAL and old["original"]["status"] == "INCOMPLETE"
    assert old["original_zip_sha256"] == "c9be09559db57ba795a893a5e706b85a13fc000e6939faf0535f4ddcdf365a9f"
    stock = recovered["arms"]["stock"]
    assert stock["verified_second_poll_timed_out"] and list(stock["cases"]) == [RECURRING + "/0"]
    assert stock["cases"][RECURRING + "/0"]["outcome"] == "fail"
    assert old["arms"]["stock"]["native_exit"] == 3
    native = recovered["arms"]["native"]["cases"]
    assert set(native) == {f"{case}/{i}" for case in CASES for i in range(12)}
    failed = {f"{SUITE}.{case}" for case, r in native.items() if r["outcome"] == "fail"}
    assert failed == EXPECTED and all(r["outcome"] in ("pass", "fail") for r in native.values())
    assert all(r["diagnostic"]["errno"] == ["EAFNOSUPPORT"] for r in native.values() if r["outcome"] == "fail")
    for name, count in (("fixed", 48), ("clock", 10)):
        row = old["arms"][name]
        assert row["status"] == "PASS" and row["source"] == PRODUCT
        assert row["case_counts"] == dict(passed=count, failed=0, skipped=0)
    result.update(original=recovered, original_zip_sha256=digest, reused=dict(
        native_ipv4_passed=16, stock_second_poll_timed_out=True, fixed_passed=48, clock_leaves_passed=8))
    save()


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
    assert name == "native" and target == "//test/syscalls:socket_ip_tcp_generic_loopback_test_native"
    case_filter = "--test_env=GTEST_FILTER=" + ":".join(sorted(EXPECTED))
    args = ["test", "--config=x86_64", "--keep_going", "--strip=never",
            "--lockfile_mode=off", "--strategy=TestRunner=local", case_filter, "--test_env=GO_TEST_WRAP_TESTV=1", "--test_sharding_strategy=disabled",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=180",
            "--remote_download_outputs=toplevel", "--test_output=errors",
            f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", target]
    (out / "executed_arguments").write_text("\n".join(args) + "\n")
    # The outer budget includes builds; the TestRunner still has only 180 seconds.
    command = ["timeout", "--signal=INT", "--kill-after=30s", "2700",
               "bazel", f"--bazelrc={ROOT / 'cork.bazelrc'}", *args]
    with (out / "console").open("wb") as stream:
        code = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT).returncode
    finished = utc()
    row = dict(source=source, target=target, native_exit=code, command_finished_at=finished,
               status="FAIL" if code == 3 else "INCOMPLETE")
    with (out / "console").open("rb") as stream:
        stream.seek(max(0, (out / "console").stat().st_size - 65536))
        progress = re.findall(rb"\[([\d,]+) / ([\d,]+)\]", stream.read())
    if progress:
        row["last_reported_actions"] = [int(number.replace(b",", b"")) for number in progress[-1]]
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
    clean = (out / "final_head").read_text() == source and not any(
        (out / filename).read_bytes().strip() for filename in ("source_before", "source_after"))
    row.update(original_status=verdict["status"], case_counts=verdict["case_counts"],
               source_clean=clean, evidence_complete=not (verdict["errors"] or verdict["missing"]),
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
    assert clean and row["evidence_complete"] and info.get("strategy") == "local"
    assert not observation.get("cachedLocally") and not info.get("cachedRemotely")
    xml, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.xml"]
    log, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.log"]
    log_text = log.read_text(errors="replace")
    nodes = list(ET.parse(xml).getroot().iter("testcase"))
    expected = EXPECTED
    actual = {case.get("classname", "") + "." + case.get("name", "") for case in nodes}
    assert len(nodes) == len(expected) and actual == expected
    assert all(case.find("skipped") is None and case.get("status") != "notrun" and
               case.get("result") != "suppressed" for case in nodes)
    marker = "OK"
    for case in expected:
        assert re.search(r"\[\s*" + marker + r"\s*\]\s+" + re.escape(case) + r"(?=\s|$)", log_text)
    row["cases"] = sorted(expected)
    assert code == 0 and observation["status"] == "PASSED" and verdict["status"] == "PASS"
    assert row["case_counts"] == dict(passed=len(expected), failed=0, skipped=0)
    row["status"] = "PASS"
    save()


result = dict(source=os.environ["GITHUB_SHA"], product=PRODUCT, stock=STOCK, base=BASE,
              started_at=utc(), status="INCOMPLETE", arms={})
save()
try:
    assert git("rev-parse", "HEAD^") == "4a3e55b3d217d699873b479945c8367183cee1e0"
    assert git("rev-parse", "HEAD~2") == ORIGINAL and git("rev-parse", "HEAD~3") == STOCK
    reuse_original()
    assert git("rev-parse", STOCK + "^") == PRODUCT
    assert git("rev-parse", PRODUCT + "^") == BASE
    sender = "pkg/tcpip/transport/tcp/snd.go"
    fixture = "test/syscalls/linux/socket_ip_tcp_generic.cc"
    assert git("diff", "--name-only", STOCK, PRODUCT) == sender
    assert git("rev-parse", STOCK + ":" + sender) == git("rev-parse", BASE + ":" + sender)
    assert git("diff", "--name-only", BASE, PRODUCT).splitlines() == [sender, fixture]
    assert os.geteuid() == 0 and os.uname().sysname == "Linux"
    assert Path("/proc/sys/net/ipv6/conf/lo/disable_ipv6").read_text().strip() == "0"
    with socket.socket(socket.AF_INET6) as sock:
        sock.bind(("::1", 0))
        assert sock.getsockname()[0] == "::1"
    result["native_preflight"] = dict(uid=0, ipv6_loopback_bind=True)
    save()
    try:
        execute("native", STOCK, "//test/syscalls:socket_ip_tcp_generic_loopback_test_native")
    except Exception as error:
        result["arms"].setdefault("native", {"status": "INCOMPLETE"})["verifier_error"] = type(error).__name__
    result["status"] = result["arms"]["native"]["status"]
finally:
    save()
assert result["status"] == "PASS"
