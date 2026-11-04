"""Compare the existing bridge syscall regression on Linux and two Sentries."""

from datetime import datetime, timezone
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import xml.etree.ElementTree as ET
import zipfile

import qualification_report as report

ROOT = Path(os.environ["RUNNER_TEMP"])
PUBLIC = ROOT / "bridge-public"
PRODUCT = "735639f573bb921490150d0c722414c124119728"
STOCK = "03e75796c4fe287faa7a5e0f6a85ae81c044f896"
BASE = "b21a9d4eea7b0066232c33c63df9cf20ed987f6f"
CASE = "NetlinkRouteTest/BridgeAddUnnamed"
DIAGNOSTIC_SOURCE = "4b290b5879af5ba7e7a3f577d0dbcbffe9e3c7ce"
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


def binaries(target, name):
    script = Path("bazel-bin/test/syscalls") / target.split(":")[1]
    line, = [line for line in script.read_text().splitlines() if line.startswith("exec ")]
    args = shlex.split(line)
    assert "--network=none" in args
    assert "--platform=" + ("native" if name == "native" else "systrap") in args
    assert "--file-access=" + ("exclusive" if name == "native" else "shared") in args
    paths = {"test_runner": args[1], "fixture": args[-1], "runsc": "release/runsc",
             "sentry": "release/gvisor-bin/gvisor_sentry"}
    records = {"wrapper": {"sha256": hashlib.sha256(script.read_bytes()).hexdigest(), "bytes": script.stat().st_size}}
    for role, relative in paths.items():
        path = Path(relative)
        assert not path.is_absolute() and ".." not in path.parts
        path = Path("bazel-bin") / path
        assert stat.S_ISREG(path.stat().st_mode) and path.stat().st_mode & 0o111
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            header = stream.read(20)
            assert header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20], "little") == 62
            digest.update(header)
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        records[role] = {"path": relative, "bytes": path.stat().st_size, "sha256": digest.hexdigest(), "elf_machine": 62}
    return records


def execute(name, source, target):
    git("checkout", "--quiet", "--detach", source)
    out = ROOT / "bridge-private" / name
    out.mkdir(parents=True, mode=0o700)
    for field, args in {
        "commit_file": ("cat-file", "commit", "HEAD"),
        "source_before": ("status", "--porcelain", "--untracked-files=no"),
    }.items():
        (out / field).write_bytes(subprocess.check_output(["git", *args]))
    raw = out / "raw.jsonl"
    args = ["test", "--config=x86_64", "--keep_going", "--strip=never",
            "--lockfile_mode=off", "--strategy=TestRunner=local", "--test_filter=NetlinkRouteTest.BridgeAddUnnamed", "--test_sharding_strategy=disabled",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=180",
            "--remote_download_outputs=toplevel", "--test_output=errors",
            f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", target]
    (out / "executed_arguments").write_text("\n".join(args) + "\n")
    command = ["timeout", "--signal=INT", "--kill-after=30s", "1800",
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
    assert row["evidence_complete"] and info.get("strategy") == "local"
    assert not observation.get("cachedLocally") and not info.get("cachedRemotely")
    row["materialized_binaries"] = binaries(target, name)
    log, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.log"]
    text = log.read_text(errors="replace")
    row["gtest_started"] = bool(re.search(r"\[\s*RUN\s*\]\s+NetlinkRouteTest\.BridgeAddUnnamed\b", text))
    relative = "test/runner/setup_container/setup_container"
    helper = Path("bazel-bin/test/syscalls") / (target.split(":")[1] + ".runfiles") / "_main" / relative
    helper_info = {"runfiles_path": relative}
    try:
        metadata = helper.stat()
        helper_info.update(kind="file" if stat.S_ISREG(metadata.st_mode) else "other", mode=oct(stat.S_IMODE(metadata.st_mode)))
        if stat.S_ISREG(metadata.st_mode):
            payload = helper.read_bytes()
            helper_info.update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
                               elf_machine=int.from_bytes(payload[18:20], "little") if payload[:6] == b"\x7fELF\x02\x01" else None)
    except OSError as error:
        helper_info.update(kind="unavailable", errno=errno.errorcode.get(error.errno, "UNKNOWN"))
    row["setup_helper"] = helper_info
    errors = []
    for error in re.findall(r'\btest "main\.test" failed: ([^\r\n]+)', text):
        observation = {"sha256": hashlib.sha256(error.encode()).hexdigest(), "message": "UNRECOGNIZED"}
        match = re.fullmatch(r"fork/exec (.+): (.+)", error)
        if match and match[1].endswith("/" + relative):
            for number, name in errno.errorcode.items():
                if match[2] == os.strerror(number):
                    observation.update(message="fork/exec <setup_container>: " + match[2], errno=name)
                    break
        elif re.fullmatch(r"exit status [0-9]{1,3}", error):
            observation["message"] = error
        elif error in ("signal: killed", "signal: segmentation fault", "signal: aborted"):
            observation["message"] = error
        errors.append(observation)
    assert len(errors) <= 1
    row["native_errors"] = errors
    save()
    xml, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.xml"]
    case, = ET.parse(xml).getroot().iter("testcase")
    assert "/".join(filter(None, (case.get("classname"), case.get("name")))) == CASE
    assert case.find("skipped") is None and case.get("status") != "notrun" and case.get("result") != "suppressed"
    if name == "stock":
        assert observation["status"] == "FAILED"
        failures = case.findall("failure")
        texts = ["".join(item.itertext()) for item in failures]
        patterns = (r"bridge\.mtu\s+Which is: 1486\s+1500", r"updated.mtu\s+Which is: 1446\s+1460")
        assert len(texts) == 2 and all(any(re.search(pattern, text) for text in texts) for pattern in patterns)
        assert code == 3 and verdict["status"] == "FAIL" and row["case_counts"] == dict(passed=0, failed=1, skipped=0)
        row.update(status="EXPECTED_FAILURE", observed_mtu=[1486, 1446], requested_mtu=[1500, 1460])
    else:
        assert code == 0 and verdict["status"] == "PASS" and row["case_counts"] == dict(passed=1, failed=0, skipped=0)
        row["status"] = "PASS"
    save()


result = dict(source=os.environ["GITHUB_SHA"], product=PRODUCT, stock=STOCK, base=BASE,
              started_at=utc(), status="INCOMPLETE", arms={}, command_timeout_seconds=1800)
save()
try:
    assert git("rev-parse", "HEAD^") == DIAGNOSTIC_SOURCE
    assert git("rev-parse", DIAGNOSTIC_SOURCE + "^") == STOCK
    assert git("diff", "--name-only", STOCK, DIAGNOSTIC_SOURCE) == "test/runner/main.go"
    expected = git("show", STOCK + ":test/runner/main.go").replace(
        '\t\tws := err.(*exec.ExitError).Sys().(syscall.WaitStatus)\n\t\tt.Errorf("test %q exited with status %d, want 0", tc.FullName(), ws.ExitStatus())',
        '\t\tt.Errorf("test %q failed: %v", tc.FullName(), err)')
    assert git("show", DIAGNOSTIC_SOURCE + ":test/runner/main.go") == expected
    data = (ROOT / "bridge-original.zip").read_bytes()
    assert len(data) == 2224 and hashlib.sha256(data).hexdigest() == "a08395b1d541ab1d7723aeb466524c4050755c69d256cc2e6b2ff7948fe096bd"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["verdict.json"]
        prior = json.loads(archive.read("verdict.json"))
    assert prior["source"] == "d4c958bc8681caf621a07098b5a5ca5b81309167" and prior["status"] == "FAIL"
    assert set(prior["arms"]) == {"native"}
    old = prior["arms"]["native"]
    assert old["source"] == STOCK and old["native_exit"] == 3
    assert old["invocation"] == "14dc23d7-1ce1-4645-a9da-40304791fa86"
    assert old["attempt"]["status"] == "FAILED" and old["attempt"]["strategy"] == "local"
    result.update(original_native=prior, diagnostic_source=DIAGNOSTIC_SOURCE,
                  original_diagnostic="https://github.com/tamird/gvisor/actions/runs/41746804630/artifacts/13284830030")
    save()
    coordinator = json.loads((ROOT / "coordinator.json").read_text())
    assert coordinator["privileged"] is True and coordinator["user"] == "0:0"
    assert coordinator["image"] == "gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
    assert os.getuid() == 0 and os.uname().sysname == "Linux" and os.uname().machine == "x86_64"
    capabilities, = re.findall(r"^CapEff:\s*([0-9a-f]+)$", Path("/proc/self/status").read_text(), re.M)
    assert all(int(capabilities, 16) & (1 << bit) for bit in (12, 21))
    assert "RUNTIME" not in os.environ
    coordinator.update(uid=os.getuid(), architecture=os.uname().machine, kernel=os.uname().release,
                       net_admin=True, sys_admin=True,
                       hostname_sha256=hashlib.sha256(socket.gethostname().encode()).hexdigest())
    result["coordinator"] = coordinator
    result["placement"] = {"compile_strategy": "remote", "test_strategy": "local", "remote_local_fallback": False}
    result["related_setup_failure"] = "https://github.com/tamird/gvisor/actions/runs/41737617085/artifacts/13279139095"
    save()
    originals = []
    for name, digest in (
        ("original.zip", "c22c2c2abc04889d426e9e8190ac528e4f2f0a78eb14bc0ad82f4137f0814323"),
        ("original-console.zip", "564f8d8a227b556efd46702000968785b776d7c427868c7b6f27c566027714b1"),
    ):
        data = (ROOT / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert archive.namelist() == ["verdict.json"]
            originals.append(json.loads(archive.read("verdict.json")))
    original, recovered = originals
    assert original["source"] == "4b64fa6de52a1b145ed86bb39585ca918d9a1153"
    assert original["product"] == PRODUCT and original["stock"] == STOCK
    assert original["status"] == "INCOMPLETE" and set(original["arms"]) == {"native"}
    native = original["arms"]["native"]
    assert native["native_exit"] == 124 and native["case_counts"] == {} and native["reports"] == []
    assert native["source"] == STOCK and native["target"] == "//test/syscalls:socket_netlink_route_test_native"
    assert native["invocation"] == "bbcaf9bd-0664-43b0-9ef9-87344330eeab"
    assert recovered["source"] == "0d5bcebaf295fa5d949b631876fadac1ddfd6d57"
    assert recovered["original"] == original and recovered["workload_repeated"] is False
    assert recovered["console_forward_complete"] and recovered["status"] == "ORIGINAL_LOGS_READ"
    console = recovered["console"]["forward"]
    assert console["sha256"] == "38f59ad42ac5fc5f43631beba4e6a7cc04d5f366aec6ec868781cd0dc99ba0ba"
    assert console["bytes"] == 129464
    result.update(original=original, original_console=console)
    save()
    assert git("rev-parse", STOCK + "^") == PRODUCT
    assert git("rev-parse", PRODUCT + "^") == BASE
    assert git("diff", "--name-only", STOCK, PRODUCT) == "pkg/tcpip/stack/bridge.go"
    assert git("rev-parse", STOCK + ":pkg/tcpip/stack/bridge.go") == git("rev-parse", BASE + ":pkg/tcpip/stack/bridge.go")
    assert git("diff", "--name-only", BASE, PRODUCT).splitlines() == [
        "pkg/tcpip/stack/bridge.go", "pkg/tcpip/stack/bridge_test.go", "test/syscalls/linux/socket_netlink_route.cc",
        "test/syscalls/linux/socket_netlink_route_util.cc", "test/syscalls/linux/socket_netlink_route_util.h",
        "test/syscalls/linux/tuntap.cc"]
    for name, source, suffix in (("native", DIAGNOSTIC_SOURCE, "native"),):
        try:
            execute(name, source, "//test/syscalls:socket_netlink_route_test_" + suffix)
        except Exception as error:
            result["arms"].setdefault(name, {"status": "INCOMPLETE"})["verifier_error"] = type(error).__name__
            save()
            if name == "native":
                break
    statuses = {name: row["status"] for name, row in result["arms"].items()}
    result["status"] = "NATIVE_DIAGNOSTIC_PASS" if statuses == {"native": "PASS"} else (
        "FAIL" if "FAIL" in statuses.values() else "INCOMPLETE")
finally:
    save()
assert result["status"] == "NATIVE_DIAGNOSTIC_PASS"
