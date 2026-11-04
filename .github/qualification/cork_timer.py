"""Check cork timeouts and flushing on Linux and both gVisor senders."""

from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
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


def reuse_original():
    path = ROOT / "cork-original.zip"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == "c9be09559db57ba795a893a5e706b85a13fc000e6939faf0535f4ddcdf365a9f"
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == ["verdict.json"]
        old = json.loads(archive.read("verdict.json"))
    assert (old["source"], old["product"], old["stock"], old["base"]) == (ORIGINAL, PRODUCT, STOCK, BASE)
    assert old["status"] == "INCOMPLETE" and set(old["arms"]) == {"native", "stock", "fixed", "clock"}
    for name in ("native", "stock"):
        row = old["arms"][name]
        assert row["status"] == "INCOMPLETE" and row["native_exit"] == 124
        assert not row["reports"] and not row["case_counts"] and "attempt" not in row
    for name, count, leaves in (("fixed", 48, 48), ("clock", 10, 8)):
        row = old["arms"][name]
        assert row["source"] == PRODUCT and row["status"] == "PASS" and row["native_exit"] == 0
        assert row["case_counts"] == dict(passed=count, failed=0, skipped=0) and len(row["cases"]) == leaves
        assert row["attempt"]["strategy"] == "remote" and row["attempt"]["status"] == "PASSED"
        assert not (row["attempt"]["cached_locally"] or row["attempt"]["cached_remotely"])
        result["arms"][name] = row
    result.update(original=old, original_zip_sha256=digest, reused_arms=["fixed", "clock"])
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
    case_filter = (f"--test_env=GTEST_FILTER={SUITE}.{RECURRING}/0" if name == "stock" else
                   "--test_filter=^Test(CorkFlushBeforeFIN|MinshallPartialSegment)$" if name == "clock" else
                   "--test_env=GTEST_FILTER=" + ":".join(f"{SUITE}.{case}/*" for case in CASES))
    args = ["test", "--config=x86_64", "--keep_going", "--strip=never",
            "--lockfile_mode=off", case_filter, "--test_env=GO_TEST_WRAP_TESTV=1", "--test_sharding_strategy=disabled",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=180",
            "--remote_download_outputs=toplevel", "--test_output=errors",
            f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", target]
    (out / "executed_arguments").write_text("\n".join(args) + "\n")
    # The outer budget includes builds; the TestRunner still has only 180 seconds.
    command = ["docker", "exec", "cork-gnu", "timeout", "--signal=INT", "--kill-after=30s", "2700",
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
    assert clean and row["evidence_complete"] and info.get("strategy") == "remote"
    assert not observation.get("cachedLocally") and not info.get("cachedRemotely")
    xml, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.xml"]
    log, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.log"]
    log_text = log.read_text(errors="replace")
    if name == "clock":
        expected = {"TestCorkFlushBeforeFIN/close", "TestCorkFlushBeforeFIN/shutdown"} | {
            f"TestMinshallPartialSegment/gso={gso}/{case}"
            for gso in ("true", "false") for case in ("full-prefix=0", "full-prefix=100", "cork-expired")
        }
        observed = set(re.findall(r"--- PASS: (Test(?:CorkFlushBeforeFIN|MinshallPartialSegment)/[^ ]+) ", log_text))
        # The slash in a nested t.Run name also emits an intermediate gso=... group.
        assert observed & expected == expected and not re.search(r"--- (?:FAIL|SKIP):", log_text)
        assert code == 0 and observation["status"] == "PASSED" and verdict["status"] == "PASS"
        row.update(status="PASS", cases=sorted(expected))
        save()
        return
    nodes = list(ET.parse(xml).getroot().iter("testcase"))
    expected = ({f"{SUITE}.{RECURRING}/0"} if name == "stock" else
                {f"{SUITE}.{case}/{i}" for case in CASES for i in range(12)})
    actual = {case.get("classname", "") + "." + case.get("name", "") for case in nodes}
    assert len(nodes) == len(expected) and actual == expected
    assert all(case.find("skipped") is None and case.get("status") != "notrun" and
               case.get("result") != "suppressed" for case in nodes)
    if name == "stock" and code == 0 and verdict["status"] == "PASS" and observation["status"] == "PASSED":
        assert row["case_counts"] == dict(passed=1, failed=0, skipped=0)
        assert re.search(r"\[\s*OK\s*\]\s+" + re.escape(next(iter(expected))) + r"(?=\s|$)", log_text)
        row.update(status="UNEXPECTED_PASS", cases=sorted(expected))
        save()
        return
    marker = "FAILED" if name == "stock" else "OK"
    for case in expected:
        assert re.search(r"\[\s*" + marker + r"\s*\]\s+" + re.escape(case) + r"(?=\s|$)", log_text)
    row["cases"] = sorted(expected)
    if name == "stock":
        assert observation["status"] == "FAILED" and code == 3 and verdict["status"] == "FAIL"
        failure, = nodes[0].findall("failure")
        text = "".join(failure.itertext()) + failure.get("message", "")
        assert re.search(r"RetryEINTR\(poll\).*?Actual:\s*0\b", text, re.S)
        assert re.search(r"socket_ip_tcp_generic\.cc:\d+: 1\b", text)
        assert row["case_counts"] == dict(passed=0, failed=1, skipped=0)
        row.update(status="EXPECTED_FAILURE", observed="second poll timed out")
    else:
        assert code == 0 and observation["status"] == "PASSED" and verdict["status"] == "PASS"
        assert row["case_counts"] == dict(passed=len(expected), failed=0, skipped=0)
        row["status"] = "PASS"
    save()


def original_reports():
    from cas_inputs import blob

    path = ROOT / "cork-original.zip"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == "fc37d8ae8d6423e8767258fca8bab02decb16647fa0b05ee45ce918ff542de71"
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == ["verdict.json"]
        old = json.loads(archive.read("verdict.json"))
    assert old["source"] == "4a3e55b3d217d699873b479945c8367183cee1e0" and old["status"] == "FAIL"
    result.update(original=old, original_zip_sha256=digest)
    save()
    # Match names in both GTest XML and the syscall runner's rendered log.
    case_re = re.compile("(" + "|".join(CASES) + r")/(\d{1,2})(?!\d)")
    allowed_files = {Path(p).name for p in git("ls-files", "test/syscalls/linux", "test/util").splitlines()}

    def failure(text):
        files = re.findall(r"(?<![a-zA-Z0-9_.-])([a-zA-Z0-9_-]+\.(?:cc|h)):(\d{1,6})\b", text)
        return {
            "locations": list(dict.fromkeys(f"{f}:{n}" for f, n in files if f in allowed_files))[:12],
            "errno": sorted({name for i, name in errno.errorcode.items() if name in text or
                             os.strerror(i).lower() in text.lower() or
                             re.search(rf"\berrno(?:\s*[:=]|\s)\s*{i}\b", text, re.I)}),
            "socket_calls": [word for word in ("NewSocketPair", "socket", "bind", "connect", "listen",
                             "setsockopt", "send", "recv", "poll", "shutdown") if re.search(r"\b" + word + r"\b", text)],
            "poll_zero": bool(re.search(r"RetryEINTR\(poll\)[\s\S]{0,2000}?\bActual:\s*0\b", text)),
            "iteration_one": bool(re.search(r"socket_ip_tcp_generic\.cc:1580:\s*1\b", text)),
        }

    def inspect(name, uuid, count):
        prior = old["arms"][name]
        assert prior["invocation"] == uuid and prior["source"] == STOCK and prior["native_exit"] == 3
        assert prior["attempt"]["strategy"] == "remote" and prior["evidence_complete"]
        assert not prior["attempt"]["cached_locally"] and not prior["attempt"]["cached_remotely"]
        row = result["arms"][name] = dict(invocation=uuid, target=prior["target"], files={}, status="INCOMPLETE")
        save()
        members = {}
        for kind in ("test.xml", "test.log"):
            try:
                record, = [r for r in prior["reports"] if r["name"] == kind]
                assert 0 < record["bytes"] <= 128 * 1024
                raw = blob(dict(hash=record["sha256"], sizeBytes=record["bytes"]), uuid)
                members[kind] = raw
                row["files"][kind] = record
            except Exception as error:
                row["files"][kind] = {"missing": type(error).__name__}
            save()
        if "test.xml" not in members:
            return
        row["xml_diagnostic"] = failure(members["test.xml"].decode(errors="replace"))
        if "test.log" in members and name == "stock":
            row["log_diagnostic"] = failure(members["test.log"].decode(errors="replace"))
        save()
        cases = {}
        for node in ET.fromstring(members["test.xml"]).iter("testcase"):
            names = node.get("classname", "") + "." + node.get("name", "")
            matches = set(case_re.findall(names))
            assert len(matches) == 1
            test, number = matches.pop()
            assert 0 <= int(number) < 12
            case = f"{test}/{number}"
            assert case not in cases
            errors = node.findall("failure") + node.findall("error")
            detail = "\n".join((e.get("message", "") + "\n" + "".join(e.itertext())) for e in errors)
            skip = node.find("skipped") is not None or node.get("status") == "notrun" or node.get("result") == "suppressed"
            cases[case] = dict(outcome="skip" if skip else "fail" if errors else "pass")
            if errors:
                cases[case]["diagnostic"] = failure(detail)
        row["cases"] = cases
        save()
        assert len(cases) == count and not any(c["outcome"] == "skip" for c in cases.values())
        assert sum(c["outcome"] == "fail" for c in cases.values()) == prior["case_counts"]["failed"]
        if "test.log" not in members:
            return
        if name == "stock":
            # Both reports are scoped to this single case; never infer from runtime.
            assert set(cases) == {RECURRING + "/0"}
            projections = [cases[RECURRING + "/0"]["diagnostic"], row["log_diagnostic"]]
            row["verified_second_poll_timed_out"] = any(p["poll_zero"] and p["iteration_one"] for p in projections)
        row["status"] = "ORIGINAL_REPORTS_READ"
        save()
    for name, uuid, count in (("stock", "4a15ad7c-8aac-439e-a3e0-dbb336aca766", 1),
                              ("native", "9afd1ff2-d2ad-4e9d-8dfa-f00ae001d8f5", 48)):
        try:
            inspect(name, uuid, count)
        except Exception as error:
            row = result["arms"].setdefault(name, dict(status="INCOMPLETE"))
            row["error"] = type(error).__name__
            save()
    if set(result["arms"]) == {"native", "stock"} and all(
            row["status"] == "ORIGINAL_REPORTS_READ" for row in result["arms"].values()):
        result["status"] = "ORIGINAL_REPORTS_READ"
        save()


result = dict(source=os.environ["GITHUB_SHA"], product=PRODUCT, stock=STOCK, base=BASE,
              started_at=utc(), status="INCOMPLETE", arms={})
save()
if sys.argv[1:] == ["--original-reports"]:
    original_reports()
    sys.exit(result["status"] != "ORIGINAL_REPORTS_READ")
try:
    assert git("rev-parse", "HEAD^") == ORIGINAL
    assert git("rev-parse", "HEAD~2") == STOCK
    reuse_original()
    assert git("rev-parse", STOCK + "^") == PRODUCT
    assert git("rev-parse", PRODUCT + "^") == BASE
    sender = "pkg/tcpip/transport/tcp/snd.go"
    fixture = "test/syscalls/linux/socket_ip_tcp_generic.cc"
    assert git("diff", "--name-only", STOCK, PRODUCT) == sender
    assert git("rev-parse", STOCK + ":" + sender) == git("rev-parse", BASE + ":" + sender)
    assert git("diff", "--name-only", BASE, PRODUCT).splitlines() == [sender, fixture]
    syscall = "//test/syscalls:socket_ip_tcp_generic_loopback_test_"
    for name, source, target in (("native", STOCK, syscall + "native"), ("stock", STOCK, syscall + "runsc_ptrace")):
        try:
            execute(name, source, target)
        except Exception as error:
            result["arms"].setdefault(name, {"status": "INCOMPLETE"})["verifier_error"] = type(error).__name__
            save()
    statuses = {name: row["status"] for name, row in result["arms"].items()}
    result["status"] = "PASS" if statuses == {"native": "PASS", "stock": "EXPECTED_FAILURE", "fixed": "PASS", "clock": "PASS"} else (
        "FAIL" if set(statuses.values()) & {"FAIL", "UNEXPECTED_PASS"} else "INCOMPLETE")
finally:
    save()
assert result["status"] == "PASS"
