"""Compare TCP_MAXSEG and TCP_INFO on both ends of a TCP connection."""

from datetime import datetime, timezone
import hashlib
import json
import platform
import socket
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

import qualification_report as report

ROOT = Path(os.environ["RUNNER_TEMP"])
PUBLIC = ROOT / "out"
PRODUCT = "36ee2797f8a32db133769117da3dd2d6af15cd82"
FIXTURE = "4de79658d3f96f311f79d56ba9833105e1b1b50a"
STOCK = "7cf14d098cc107c18a850e9f02d90cc642a58add"
BASE = "6dcadf4a84a07f69e8eb5f1ea9d7b2903bd03bd7"
SUITE = "AllInetTests/SimpleTcpSocketTest"
CASES = ("SetMaxSeg", "MaxSegDefault", "SetMaxSegFailsForInvalidMSSValues")
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
    out = ROOT / "private" / name
    out.mkdir(parents=True)
    for field, args in {
        "commit_file": ("cat-file", "commit", "HEAD"),
        "source_before": ("status", "--porcelain", "--untracked-files=no"),
    }.items():
        (out / field).write_bytes(subprocess.check_output(["git", *args]))
    raw = out / "raw.jsonl"
    cases = CASES[:1] if name == "stock" else CASES
    case_filter = "--test_filter=" + ":".join(f"{SUITE}.{case}/*" for case in cases)
    args = ["test", "--config=x86_64", "--lockfile_mode=off", "--jobs=4",
            "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
            "--strategy=TestRunner=local", "--remote_executor=", "--remote_cache=", "--bes_backend=",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled",
            case_filter, "--test_timeout=180", "--test_output=errors",
            f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", target]
    (out / "executed_arguments").write_text("\n".join(args) + "\n")
    command = ["timeout", "--signal=INT", "--kill-after=30s", "2400",
               "bazel", "--batch", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", *args]
    if name == "native":
        # The native runner keeps the verified dual-stack loopback namespace
        # without CAP_NET_ADMIN. --batch carries this bound through TestRunner.
        command = ["setpriv", "--bounding-set=-net_admin", "--", *command]
    with (out / "console").open("wb") as stream:
        code = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT).returncode
    finished = utc()
    row = dict(source=source, target=target, native_exit=code, command_finished_at=finished,
               status="FAIL" if code == 3 else "INCOMPLETE")
    result["arms"][name] = row
    console = (out / "console").read_bytes()
    row["console_sha256"] = hashlib.sha256(console).hexdigest()
    row["console_bytes"] = len(console)
    row["diagnostic_classes"] = [term for term in (
        "No space left on device", "no such package", "Error downloading", "not found",
        "undeclared inclusion", "No such file or directory", "unrecognized command-line option",
        "setting up network", "Permission denied", "Operation not permitted",
        "Address family not supported", "undefined reference", "GoStandardLibraryAnalysis",
        "FilterStdPackages", "failed to build", "cannot find", "no matching toolchains",
    ) if term.encode() in console]
    if code not in (0, 3):
        diagnostics = []
        for line in console.decode(errors="replace").splitlines():
            m = re.search(r"((?:tcp_socket\.cc|endpoint\.go|vdso\.cc|BUILD(?:\.bazel)?):\d+(?::\d+)?: (?:fatal )?error: [^;\n]{1,220})", line)
            if m and not re.search(r"(?:https?://|grpc|/home/|--|\b\w+=)", m[1]):
                diagnostics.append(m[1])
        row["build_diagnostic"] = diagnostics[:4]
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
    row["test_attempts"] = len(attempts)
    save()
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
    assert row["evidence_complete"] and info.get("strategy") == "local"
    assert not observation.get("cachedLocally") and not info.get("cachedRemotely")
    xml, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.xml"]
    log, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.log"]
    log_text = log.read_text(errors="replace")
    nodes = list(ET.parse(xml).getroot().iter("testcase"))
    expected = {f"{SUITE}.{case}/{i}" for case in cases for i in range(2)}
    actual = {case.get("classname", "") + "." + case.get("name", "") for case in nodes}
    row["cases"] = sorted(expected & actual)
    row["observations"] = [dict(family=int(family), role=role, tcp_maxseg=int(value),
                                 tcpi_snd_mss=int(snd), tcpi_options=int(options))
                           for family, role, value, snd, options in re.findall(
                               r"MSS_OBSERVATION family=(2|10) role=(active|accepted) get=(\d+) snd=(\d+) options=(\d+)",
                               log_text)]
    readings = row["observations"]
    save()
    assert len(nodes) == len(expected) and actual == expected
    assert all(case.find("skipped") is None and case.find("error") is None and
               case.get("status") != "notrun" and case.get("result") != "suppressed" for case in nodes)
    assert len(readings) == 4 and {(r["family"], r["role"]) for r in readings} == {
        (f, role) for f in (2, 10) for role in ("active", "accepted")}
    assert all(536 < r["tcpi_snd_mss"] <= 1024 for r in readings)
    marker = "FAILED" if name == "stock" else "OK"
    for case in expected:
        assert re.search(r"\[\s*" + marker + r"\s*\]\s+" + re.escape(case) + r"(?=\s|$)", log_text)
    if name == "stock":
        assert observation["status"] == "FAILED" and code == 3 and verdict["status"] == "FAIL"
        assert row["case_counts"] == dict(passed=0, failed=2, skipped=0)
        assert all(r["tcp_maxseg"] == 536 for r in readings)
        for node in nodes:
            failures = node.findall("failure")
            assert failures
            for fail in failures:
                message = fail.get("message") or "".join(fail.itertext())
                assert "optval" in message and "info.tcpi_snd_mss" in message and "536" in message
        row["status"] = "EXPECTED_FAILURE"
    else:
        assert code == 0 and observation["status"] == "PASSED" and verdict["status"] == "PASS"
        assert row["case_counts"] == dict(passed=len(expected), failed=0, skipped=0)
        assert all(r["tcp_maxseg"] == r["tcpi_snd_mss"] for r in readings)
        row["status"] = "PASS"
    save()


result = dict(source=os.environ["GITHUB_SHA"], product=PRODUCT, stock=STOCK, base=BASE,
              fixture=FIXTURE, started_at=utc(), kernel=platform.release(), machine=platform.machine(),
              status="INCOMPLETE", arms={})
save()
try:
    assert platform.machine() == "x86_64" and platform.system() == "Linux"
    assert git("rev-parse", "HEAD^") == STOCK and git("rev-parse", STOCK + "^") == FIXTURE
    assert git("rev-parse", FIXTURE + "^") == PRODUCT and git("rev-parse", PRODUCT + "^") == BASE
    sender = "pkg/tcpip/transport/tcp/endpoint.go"
    fixture = "test/syscalls/linux/tcp_socket.cc"
    assert git("diff", "--name-only", STOCK, FIXTURE) == sender
    assert git("rev-parse", STOCK + ":" + sender) == git("rev-parse", BASE + ":" + sender)
    assert git("diff", "--name-only", FIXTURE, PRODUCT) == fixture
    assert git("diff", "--name-only", BASE, PRODUCT).splitlines() == [sender, fixture]
    for family, endpoint in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        with socket.socket(family) as sock:
            sock.bind((endpoint, 0))
    result["dual_stack_loopback"] = True
    target = "//test/syscalls:tcp_socket_test_"
    for name, source, owner in (("native", STOCK, "native"), ("stock", STOCK, "runsc_systrap_directfs"),
                                ("fixed", FIXTURE, "runsc_systrap_directfs")):
        try:
            execute(name, source, target + owner)
        except Exception as error:
            arm = result["arms"].setdefault(name, {"status": "INCOMPLETE"})
            arm["verifier_error"] = type(error).__name__
            save()
            if arm.get("native_exit") not in (0, 3) and not arm.get("test_attempts"):
                break
    statuses = {name: row["status"] for name, row in result["arms"].items()}
    result["status"] = ("PASS" if statuses == {"native": "PASS", "stock": "EXPECTED_FAILURE", "fixed": "PASS"}
                        else "FAIL" if "FAIL" in statuses.values() else "INCOMPLETE")
finally:
    save()
assert result["status"] == "PASS"
