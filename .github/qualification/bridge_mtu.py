"""Compare the existing bridge GTest on Linux and source-built Sentries."""

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

import qualification_report as report

ROOT = Path(os.environ["RUNNER_TEMP"])
PUBLIC = ROOT / "bridge-public"
PRODUCT = "735639f573bb921490150d0c722414c124119728"
STOCK = "03e75796c4fe287faa7a5e0f6a85ae81c044f896"
BASE = "b21a9d4eea7b0066232c33c63df9cf20ed987f6f"
OCI = "//test/rtnetlink/linux:source_bridge_mtu"
FIXTURE = "//test/syscalls/linux:socket_netlink_route_test"
CASE = "NetlinkRouteTest/BridgeAddUnnamed"
CARRIER = os.environ["GITHUB_SHA"]
ORIGINAL_CARRIER = "f589199cf6173ee69948c3790e2b0d09d4f999a2"
RECOVERY_CARRIER = "5e497343c1eec5fb7ebcac610979e6f7d4fdb341"
STAGED = Path("test/rtnetlink/linux/bridge-release")
STAGED_FIXTURE = Path("test/rtnetlink/linux/bridge-fixture")
RELEASE_FILES = {"runsc", "containerd-shim-runsc-v1", *("gvisor-bin/" + name for name in (
    "gvisor_sentry", "checkpointgofer", "runsc-metric-server", "runsc-fd-parking",
    "gvisor-sentry-prewarmer", "gvisor-proc-umounter"))}

SECRETS = [value for key in ("GH_TOKEN", "GITHUB_TOKEN", "BUILDBUDDY_API_KEY")
           if (value := os.environ.get(key))]


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args]).decode().strip()


def save() -> None:
    result["verdict_available_at"] = utc()
    data = json.dumps(result, indent=2, sort_keys=True) + "\n"
    assert len(data.encode()) < 128 * 1024
    assert not any(value in data or json.dumps(value)[1:-1] in data for value in SECRETS)
    assert not re.search(r"(?i)(?:authorization|x-buildbuddy-api-key)\s*[:=]|github_pat_|gh[pousr]_", data)
    (PUBLIC / "verdict.json").write_text(data)


def boundary(log: str) -> dict:
    signals = ("setting up network", "creating default loopback interface", "creating loopback link and routes",
               "containerManager.SetNetworkArgs", "connection reset by peer", "broken pipe", "EOF",
               "the sandbox exited before the container started", "RTNETLINK answers", "Operation not permitted",
               "Permission denied", "No such file or directory", "No such device", "command not found",
               "unshare failed", "unshare:", "mount failed", "must be superuser", "failed to setup",
               "Cannot find device", "Invalid argument", "Operation not supported", "File exists", "SKIP:",
               "no space left on device", "exit status", "failed to fetch", "failed to download",
               "checklocks", "must be held", "unexpectedly held", "not held", "build did not complete")
    return {"signals": [sig for sig in signals if sig.casefold() in log.casefold()],
            "source_locations": sorted(set(re.findall(
                r"(?:socket_netlink_route\.cc|packaged_bridge\.py|test-setup\.sh|test-xml-generator\.sh):(?: line )?\d{1,5}\b", log)))[:12],
            "permission_errors": [{
                "line_sha256": hashlib.sha256(line.encode()).hexdigest(),
                "operations": [word for word in ("open", "opening", "write", "writing", "read", "reading",
                    "create", "creating", "delete", "deleting", "remove", "removing", "rename", "execute",
                    "exec", "fork", "cd", "chmod", "chown", "stat", "access", "copy", "copying")
                    if re.search(r"\b" + word + r"\b", line, re.I)],
                "known_files": [name for name in ("test.log", "test.xml", "test-setup.sh", "test-xml-generator.sh",
                    "packaged_bridge.py", "bridge-test", "config.json", "gvisor_sentry", "runsc")
                    if re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])", line)],
                "errno": "EACCES",
            } for line in log.splitlines() if "permission denied" in line.casefold()][:8]}


def reuse_native() -> dict:
    data = (ROOT / "bridge-original.zip").read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    assert digest == "606c448c594c39e76415bcb21d4a7d5fc97c3870efb738003fd3570f79a476fe"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["verdict.json"]
        original = json.loads(archive.read("verdict.json"))
    assert original["source"] == ORIGINAL_CARRIER and original["product"] == PRODUCT and original["stock"] == STOCK
    assert original["status"] == "INCOMPLETE" and set(original["arms"]) == {"native", "stock"}
    native = original["arms"]["native"]
    assert native["source"] == ORIGINAL_CARRIER and native["product"] == STOCK and native["target"] == OCI
    assert native["status"] == "PASS" and native["native_exit"] == 0 and native["reporter_complete"]
    assert native["case_counts"] == dict(passed=1, failed=0, skipped=0)
    assert native["attempt"]["strategy"] == "local" and native["attempt"]["status"] == "PASSED"
    assert not native["attempt"]["cached_locally"] and not native["attempt"]["cached_remotely"]
    assert native["launched_binaries"] == {"fixture": "ef96e43c017ed2166b5054d6021a92a12581d86742047bba110896e60625c25b"}
    stock = original["arms"]["stock"]
    assert stock["native_exit"] == 36 and stock["attempt"]["status"] == "FAILED" and not stock["reporter_complete"]
    result["retained_original"] = {"run": 41751798003, "artifact": 13285349172, "zip_sha256": digest,
                                   "status": original["status"], "native": native, "stock": stock}
    save()
    return original["source_builds"]["stock"]


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stage_release(name: str, source: str) -> None:
    if STAGED.exists():
        assert not STAGED.is_symlink() and {p.relative_to(STAGED).as_posix() for p in STAGED.rglob("*") if p.is_file()} == RELEASE_FILES
        shutil.rmtree(STAGED)
    git("checkout", "--quiet", "--detach", source)
    assert git("rev-parse", "HEAD") == source and not git("status", "--porcelain", "--untracked-files=no")
    private = ROOT / "bridge-private" / (name + "-release")
    private.mkdir()
    raw = private / "build.jsonl"
    row = {"source": source, "targets": ["//:release", FIXTURE], "requested_spawn_strategy": "remote", "remote_accept_cached": True}
    result.setdefault("source_builds", {})[name] = row
    save()
    with (private / "console").open("wb") as stream:
        row["exit"] = subprocess.run(["docker", "exec", "bridge-gnu", "timeout", "--signal=INT", "--kill-after=30s",
            "1800", "bazel", "--batch", f"--bazelrc={ROOT / 'bridge.bazelrc'}", "build", "--config=x86_64",
            "--strip=never", "--lockfile_mode=off", "--remote_download_outputs=toplevel",
            f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", "//:release", FIXTURE],
            stdout=stream, stderr=subprocess.STDOUT).returncode
    row["finished_at"] = utc()
    row["command_boundary"] = boundary((private / "console").read_text(errors="replace"))
    save()
    row["completed_targets"] = {}
    for line in raw.open():
        event = json.loads(line)
        if "started" in event:
            identifier = event["started"]["uuid"]
            assert re.fullmatch(r"[a-f0-9-]{36}", identifier)
            row["invocation"] = identifier
        if "completed" in event and (identity := event.get("id", {}).get("targetCompleted", {})):
            if identity.get("label") in row["targets"] and not identity.get("aspect"):
                assert identity["label"] not in row["completed_targets"]
                assert re.fullmatch(r"[a-f0-9]{64}", identity["configuration"]["id"])
                row["completed_targets"][identity["label"]] = {
                    "configuration": identity["configuration"]["id"], "success": event["completed"].get("success", False)}
        if "buildMetrics" in event:
            runners = event["buildMetrics"].get("actionSummary", {}).get("runnerCount", [])
            row["runners"] = {v["name"]: v.get("count", 0) for v in runners if v.get("name") in
                              ("remote", "remote cache hit", "internal", "local", "linux-sandbox", "worker")}
    save()
    assert row["exit"] == 0 and "invocation" in row and not git("status", "--porcelain", "--untracked-files=no")
    assert set(row["completed_targets"]) == set(row["targets"]) and all(item["success"] for item in row["completed_targets"].values())
    release = Path("bazel-bin/release")
    produced = {p.relative_to(release).as_posix() for p in release.rglob("*") if p.is_file()}
    assert produced == RELEASE_FILES
    row["binaries"] = {rel: checksum(release / rel) for rel in sorted(produced)}
    fixture = Path("bazel-bin/test/syscalls/linux/socket_netlink_route_test")
    for path in (fixture, *(release / rel for rel in sorted(produced))):
        with path.open("rb") as stream:
            header = stream.read(20)
        assert header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20], "little") == 62, path.name
        assert path.stat().st_size > 20 and os.access(path, os.X_OK), path.name
    row["fixture"] = {"sha256": checksum(fixture), "bytes": fixture.stat().st_size, "elf_machine": 62}
    assert row["fixture"]["sha256"] == "ef96e43c017ed2166b5054d6021a92a12581d86742047bba110896e60625c25b"
    shutil.copy2(fixture, STAGED_FIXTURE)
    assert checksum(STAGED_FIXTURE) == row["fixture"]["sha256"]
    with (release / "runsc").open("rb") as runtime:
        assert runtime.read(4) == b"\x7fELF"
    shutil.copytree(release, STAGED)
    assert all(os.access(STAGED / rel, os.X_OK) and checksum(STAGED / rel) == value for rel, value in row["binaries"].items())
    save()



def execute(name: str, source: str) -> None:
    target = OCI
    product = source
    source = CARRIER
    git("checkout", "--quiet", "--detach", source)
    out = ROOT / "bridge-private" / name
    out.mkdir(parents=True)
    for field, args in {
        "commit_file": ("cat-file", "commit", "HEAD"),
        "source_before": ("status", "--porcelain", "--untracked-files=no"),
    }.items():
        (out / field).write_bytes(subprocess.check_output(["git", *args]))
    raw = out / "raw.jsonl"
    args = ["test", "--config=x86_64", "--keep_going", "--strip=never",
            "--lockfile_mode=off", "--test_sharding_strategy=disabled",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=360",
            "--remote_download_outputs=toplevel", "--test_output=errors",
            f"--test_env=BRIDGE_MODE={name}",
            "--strategy=TestRunner=local",
            f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", target]
    args = [*args[:-1], "--remote_cache=", "--remote_executor=", "--bes_backend=",
            "--build_tag_filters=", "--test_tag_filters=", target]
    command = ["env", "-i", "HOME=" + os.environ["HOME"], "USER=" + os.environ["USER"],
               "PATH=" + os.environ["PATH"], "timeout", "--signal=INT", "--kill-after=30s", "780",
               shutil.which("bazel"), "--batch", *args]
    (out / "executed_arguments").write_text("\n".join(args) + "\n")
    with (out / "console").open("wb") as stream:
        code = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT).returncode
    finished = utc()
    row = dict(source=source, product=product, target=target, native_exit=code, command_finished_at=finished,
               status="FAIL" if code == 3 else "INCOMPLETE")
    result["arms"][name] = row
    save()
    (out / "native_exit").write_text(str(code))
    (out / "source_after").write_text(git("status", "--porcelain", "--untracked-files=no"))
    (out / "final_head").write_text(git("rev-parse", "HEAD"))
    (out / "targets").write_text(target + "\n")
    events = out / "events.jsonl"
    console = (out / "console").read_text(errors="replace")
    progress = re.findall(r"\[\s*([0-9,]+)\s*/\s*([0-9,]+)\s*\]", console)
    row["last_action_count"] = [int(v.replace(",", "")) for v in progress[-1]] if progress else []
    row["timeout"] = code == 124
    row["command_boundary"] = boundary(console)
    save()
    captured = report.capture(raw, events, Path("bazel-out"), out / "reports")
    row["reports"] = [{key: item[key] for key in ("name", "sha256", "bytes")} for item in captured["reports"]]
    row["capture_boundary"] = boundary("\n".join(captured["errors"]))
    logs = [out / item["path"] for item in captured["reports"] if item["name"] == "test.log"]
    log = logs[0].read_text(errors="replace") if len(logs) == 1 else ""
    row["test_boundary"] = boundary(log)
    returns = [int(value) for value in re.findall(r"(?m)^BRIDGE_CHILD_RETURN (-?[0-9]{1,3})$", log)]
    row["child_returncodes"] = returns
    row["child_signals"] = [-value for value in returns if value < 0]
    row["child_timeout"] = "BRIDGE_CHILD_TIMEOUT" in log.splitlines()
    row["xml_boundary"] = [boundary((out / item["path"]).read_text(errors="replace"))
                           for item in captured["reports"] if item["name"] == "test.xml"]
    row["report_permissions"] = {}
    for filename in ("test.log", "test.xml", "test.outputs", "test.outputs_manifest"):
        path = Path("bazel-testlogs/test/rtnetlink/linux/source_bridge_mtu") / filename
        try:
            stat = path.stat()
            row["report_permissions"][filename] = dict(mode=oct(stat.st_mode & 0o7777), uid=stat.st_uid,
                                                       gid=stat.st_gid, bytes=stat.st_size)
        except OSError as error:
            row["report_permissions"][filename] = {"errno": error.errno}
    row["launched_binaries"] = dict(re.findall(r"(?m)^BRIDGE_BINARY (fixture|runsc|gvisor_sentry) ([a-f0-9]{64})$", log))
    save()
    (out / "capture_exit").write_text("1" if captured["errors"] else "0")
    context = {key: key for key in ("commit_file", "source_before", "source_after", "final_head",
                                   "executed_arguments", "native_exit", "capture_exit")}
    context.update(expected_source=source, command_kind="test", execution_finished_at=finished)
    report.write_json(out / "context.json", context)
    verdict = report.verdict(events, report.manifest_path(events), out / "context.json", out / "targets", ())
    row.update(original_status=verdict["status"], case_counts=verdict["case_counts"],
               source_clean=not (out / "source_before").read_text().strip() and not (out / "source_after").read_text().strip(),
               reporter_complete=not (verdict["errors"] or verdict["missing"]),
               reporting_elapsed_seconds=verdict["reporting_elapsed_seconds"],
               execution_to_verdict_seconds=verdict["execution_to_verdict_seconds"])
    records = [json.loads(line) for line in events.read_text().splitlines()]
    starts = [item["started"]["uuid"] for item in records if "started" in item]
    assert len(starts) == 1 and re.fullmatch(r"[a-f0-9-]{36}", starts[0])
    row["invocation"] = starts[0]
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
    if observation["status"] == "FAILED":
        row["status"] = "FAIL"
    save()
    assert git("rev-parse", "HEAD") == source
    assert not (out / "source_before").read_text().strip() and not (out / "source_after").read_text().strip()
    assert info.get("strategy") == "local"
    assert not observation.get("cachedLocally") and not info.get("cachedRemotely")
    built = result["source_builds"]["fixed" if name == "fixed" else "stock"]
    expected = {"fixture": built["fixture"]["sha256"]}
    if name != "native":
        expected.update(runsc=built["binaries"]["runsc"], gvisor_sentry=built["binaries"]["gvisor-bin/gvisor_sentry"])
    assert row["launched_binaries"] == expected, row["launched_binaries"]
    assert row["report_permissions"]["test.log"].get("uid") == os.getuid()
    assert returns == [1 if name == "stock" else 0] and not row["child_timeout"]
    assert row["reporter_complete"]
    xml, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.xml"]
    case, = ET.parse(xml).getroot().iter("testcase")
    assert "/".join((case.get("classname", ""), case.get("name", ""))) == CASE
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

result = dict(source=CARRIER, product=PRODUCT, stock=STOCK, base=BASE,
              started_at=utc(), status="INCOMPLETE", arms={}, source_builds={},
              placement={"build": "remote; cache accepted", "test": "local; uncached", "coordinator": "native AMD64 Linux"},
              originals=[
                  {"run": 41743992378, "artifact": 13283495690, "status": "FAIL", "gtest_started": False,
                   "zip_sha256": "a08395b1d541ab1d7723aeb466524c4050755c69d256cc2e6b2ff7948fe096bd"},
                  {"run": 41748857558, "artifact": 13285116244, "status": "FAIL", "gtest_started": False,
                   "zip_sha256": "8e4965fa1f4e2d9196341db59cf8a22efa13e61fe30a164f2edcc93a314a970a"},
                  {"run": 41750123884, "artifact": 13284988753, "startup_errno": "EACCES", "workload_repeated": False,
                   "zip_sha256": "1bb72d872742cc8c070cf56a1ac46a2c6817057008e25ba25b60279b4993dd70"},
                  {"run": 41753719629, "artifact": 13286388599, "native_exit": 36,
                   "test_log_uid": 0, "test_log_mode": "0600", "client_uid": 1001,
                   "zip_sha256": "a7648e8d56470cb81c502066c7f701637bdcc0bfda37bdc1ae88b66fafba3129"}])
save()
try:
    assert git("rev-parse", "HEAD^") == RECOVERY_CARRIER and git("rev-parse", RECOVERY_CARRIER + "^") == ORIGINAL_CARRIER
    assert git("rev-parse", ORIGINAL_CARRIER + "^") == STOCK
    assert git("rev-parse", STOCK + "^") == PRODUCT and git("rev-parse", PRODUCT + "^") == BASE
    assert git("diff", "--name-only", STOCK, PRODUCT) == "pkg/tcpip/stack/bridge.go"
    assert git("rev-parse", STOCK + ":pkg/tcpip/stack/bridge.go") == git("rev-parse", BASE + ":pkg/tcpip/stack/bridge.go")
    assert git("diff", "--name-only", BASE, PRODUCT).splitlines() == [
        "pkg/tcpip/stack/bridge.go", "pkg/tcpip/stack/bridge_test.go",
        "test/syscalls/linux/socket_netlink_route.cc", "test/syscalls/linux/socket_netlink_route_util.cc",
        "test/syscalls/linux/socket_netlink_route_util.h", "test/syscalls/linux/tuntap.cc"]
    files = ["test/syscalls/linux/" + name for name in (
        "BUILD", "socket_netlink_route.cc", "socket_netlink_route_util.cc", "socket_netlink_route_util.h")]
    result["fixture_blobs"] = {path: git("rev-parse", STOCK + ":" + path) for path in files}
    for path, blob in result["fixture_blobs"].items():
        assert git("rev-parse", PRODUCT + ":" + path) == git("rev-parse", CARRIER + ":" + path) == blob
    result["coordinator"] = dict(kernel=os.uname().release, architecture=os.uname().machine, uid=os.getuid(),
                                 runtime="systrap", network="none", fixture_donor="396038636")
    assert os.uname().sysname == "Linux" and os.uname().machine == "x86_64"
    save()
    original_build = reuse_native()
    stage_release("stock", STOCK)
    assert result["source_builds"]["stock"]["binaries"] == original_build["binaries"]
    assert result["source_builds"]["stock"]["fixture"] == original_build["fixture"]
    execute("stock", STOCK)
    assert result["arms"]["stock"]["status"] == "EXPECTED_FAILURE"
    stage_release("fixed", PRODUCT)
    execute("fixed", PRODUCT)
    assert result["arms"]["fixed"]["status"] == "PASS"
    result["status"] = "PASS"
except (AssertionError, KeyError, OSError, ValueError, ET.ParseError, subprocess.CalledProcessError) as error:
    result["verifier_error"] = type(error).__name__
    if any(row.get("status") == "FAIL" for row in result["arms"].values()):
        result["status"] = "FAIL"
finally:
    save()
assert result["status"] == "PASS"
