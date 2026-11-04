"""Compare the TCP_INFO flags with the options on the same connection's SYN-ACK."""

from datetime import datetime, timezone
import hashlib
import json
import platform
import os
from pathlib import Path
import re
import subprocess
import traceback
import xml.etree.ElementTree as ET

import qualification_report as report

ROOT = Path(os.environ["RUNNER_TEMP"])
PUBLIC = ROOT / "out"
PRODUCT = "70e7896ace3c40297c91f02abfb245527e59523f"
FIXTURE = "3b165d9482a6cbc378d7b56eb59ca6dee4706bbb"
STOCK = "58a1cb062d1a419bed5e55f356456df7de60b44e"
BASE = "6dcadf4a84a07f69e8eb5f1ea9d7b2903bd03bd7"
SUITE = "TestTCPInfoOptions"
CASES = {"Neither": 0, "Timestamps": 1, "SACK": 2, "Both": 3}
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
    args = ["test", "--config=x86_64", "--lockfile_mode=off", "--jobs=4",
            "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
            "--strategy=TestRunner=local", "--remote_executor=", "--remote_cache=", "--bes_backend=",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled",
            "--test_env=GO_TEST_WRAP_TESTV=1", f"--test_filter=^{SUITE}$", "--test_timeout=240", "--test_output=errors",
            f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", target]
    (out / "executed_arguments").write_text("\n".join(args) + "\n")
    command = ["timeout", "--signal=INT", "--kill-after=30s", "2400",
               "bazel", "--batch", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", *args]
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
            m = re.search(r"((?:tcp_info_test\.go|tcpip\.go|netstack\.go|endpoint\.go|vdso\.cc|BUILD(?:\.bazel)?):\d+(?::\d+)?: (?:fatal )?error: [^;\n]{1,220})", line)
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
    xml, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.xml"]
    log, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.log"]
    log_text = log.read_text(errors="replace")
    nodes = list(ET.parse(xml).getroot().iter("testcase"))
    expected = {SUITE, *(f"{SUITE}/{case}" for case in CASES)}
    cases = {}
    for node in nodes:
        case = node.get("name", "")
        # Bazel's fallback XML contains only the outer runner. Do not give
        # it credit for a case: a detailed XML or the exact Go result is needed.
        if case in expected:
            assert case not in cases
            cases[case] = ("SKIP" if node.find("skipped") is not None or
                           node.get("status") == "notrun" or node.get("result") == "suppressed"
                           else "FAIL" if node.find("failure") is not None or node.find("error") is not None
                           else "PASS")
    markers = {case: re.findall(r"(?:^|\s)--- (PASS|FAIL|SKIP): " + re.escape(case) + r" \(", log_text)
               for case in expected}
    samples = [dict(offer=int(offer), wire_timestamps=ts == "true", wire_sack=sack == "true", reported=int(got))
               for offer, ts, sack, got in re.findall(
                   r"TCP_INFO_WIRE offer=([0-3]) ts=(true|false) sack=(true|false) reported=([0-3])\b", log_text)]
    row.update(xml_cases=cases, go_results=markers, observations=samples)
    save()
    assert row["evidence_complete"] and info.get("strategy") == "local"
    assert not observation.get("cachedLocally") and not info.get("cachedRemotely")
    assert len(samples) == 4 and {v["offer"] for v in samples} == set(CASES.values())
    assert all(v["wire_timestamps"] + 2 * v["wire_sack"] == v["offer"] for v in samples)
    for v in samples:
        assert v["reported"] == (0 if name == "stock" else v["offer"])
    expected_results = {case: ("FAIL" if name == "stock" and case != SUITE + "/Neither" else "PASS")
                        for case in expected}
    for case, status in expected_results.items():
        assert cases.get(case, status) == status and all(m == status for m in markers[case])
        assert cases.get(case) == status or markers[case] == [status]
    if name == "stock":
        assert code == 3 and observation["status"] == "FAILED" and verdict["status"] == "FAIL"
        row["status"] = "EXPECTED_FAILURE"
    else:
        assert code == 0 and observation["status"] == "PASSED" and verdict["status"] == "PASS"
        row["status"] = "PASS"
    save()


result = dict(source=os.environ["GITHUB_SHA"], product=PRODUCT, stock=STOCK, base=BASE,
              fixture=FIXTURE, started_at=utc(), kernel=platform.release(), machine=platform.machine(),
              status="INCOMPLETE", arms={})
save()
try:
    assert platform.machine() == "x86_64" and platform.system() == "Linux"
    result["packetimpact_helpers"] = {name: os.access("/sbin/" + name, os.X_OK)
                                      for name in ("iptables-nft", "ip6tables-nft")}
    save()
    assert all(result["packetimpact_helpers"].values())
    assert git("rev-parse", "HEAD^") == STOCK and git("rev-parse", STOCK + "^") == FIXTURE
    assert git("rev-parse", FIXTURE + "^") == PRODUCT and git("rev-parse", PRODUCT + "^") == BASE
    runtime = ["pkg/sentry/socket/netstack/netstack.go", "pkg/tcpip/tcpip.go", "pkg/tcpip/transport/tcp/endpoint.go"]
    fixture = "test/packetimpact/tests/tcp_info_test.go"
    abi = "pkg/abi/linux/tcp.go"
    assert git("diff", "--name-only", STOCK, FIXTURE).splitlines() == runtime
    assert git("diff", "--name-only", STOCK, BASE).splitlines() == [abi, fixture]
    assert git("diff", "--name-only", FIXTURE, PRODUCT) == fixture
    assert git("diff", "--name-only", BASE, PRODUCT).splitlines() == [abi, *runtime, fixture]
    target = "//test/packetimpact/tests:tcp_info_"
    for name, source, owner in (("native", STOCK, "native_test"), ("stock", STOCK, "netstack_test"),
                                ("fixed", FIXTURE, "netstack_test")):
        try:
            execute(name, source, target + owner)
        except Exception as error:
            arm = result["arms"].setdefault(name, {"status": "INCOMPLETE"})
            arm["verifier_error"] = type(error).__name__
            arm["verifier_lines"] = [fr.lineno for fr in traceback.extract_tb(error.__traceback__) if Path(fr.filename).name == Path(__file__).name]
            save()
            if arm.get("native_exit") not in (0, 3) and not arm.get("test_attempts"):
                break
    statuses = {name: row["status"] for name, row in result["arms"].items()}
    result["status"] = ("PASS" if statuses == {"native": "PASS", "stock": "EXPECTED_FAILURE", "fixed": "PASS"}
                        else "FAIL" if "FAIL" in statuses.values() else "INCOMPLETE")
finally:
    save()
assert result["status"] == "PASS"
