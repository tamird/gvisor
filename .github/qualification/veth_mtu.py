"""Compare both veth endpoints using the existing setlink and veth unit tests."""

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

import qualification_report as report

ROOT = Path(os.environ["RUNNER_TEMP"])
PUBLIC = ROOT / "veth-public"
PRODUCT = "11004be74a5724b2cc3769eb41c9be31e699c432"
STOCK = "e8489c86088a020aba72296a60c2be1c75648fbd"
BASE = "400bd2c81dcc0349033a8218bdf38e239d256678"
NOGO = "//pkg/tcpip/link/veth:veth_nogo"
PUBLISHED = "dfbc2ca7059c9f7606eab8777a7804c79e3f1427"
OCI = "//test/rtnetlink/linux:source_veth_setlink"
CARRIER = os.environ["GITHUB_SHA"]
STAGED = Path("test/rtnetlink/linux/veth-release")
RELEASE_FILES = {"runsc", "containerd-shim-runsc-v1", *("gvisor-bin/" + name for name in (
    "gvisor_sentry", "checkpointgofer", "runsc-metric-server", "runsc-fd-parking",
    "gvisor-sentry-prewarmer", "gvisor-proc-umounter"))}

EXPECTED = {
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


def boundary(log):
    signals = ("setting up network", "creating default loopback interface", "creating loopback link and routes",
               "containerManager.SetNetworkArgs", "connection reset by peer", "broken pipe", "EOF",
               "the sandbox exited before the container started", "RTNETLINK answers", "Operation not permitted",
               "Permission denied", "No such file or directory", "No such device", "command not found",
               "unshare failed", "unshare:", "mount failed", "must be superuser", "failed to setup",
               "Cannot find device", "Invalid argument", "Operation not supported", "File exists", "SKIP:",
               "no space left on device", "exit status", "failed to fetch", "failed to download",
               "checklocks", "must be held", "unexpectedly held", "not held", "build did not complete")
    ops = ("mount -t tmpfs test /var/run", "ip link add name test_veth01 type veth peer name test_veth02",
           "ip link set test_veth01 mtu 3000", "ip link set test_veth02 mtu 1460",
           "ip link del name test_veth01", "ip link set test_veth01 name test_veth03",
           "ip link set dev test_veth01 address 1a:2a:3a:4a:5a:6a")
    locations = re.findall(r"(setlink_test\.sh|rtnetlink_test\.sh|setup_container\.cc|veth(?:_test)?\.go|runner/main\.go):(?: line )?(\d{1,5})\b", log)
    stages = [(log.rfind("+ " + op), op) for op in ops if "+ " + op in log]
    checks = re.findall(r"\[\[ ([0-9]{1,5}) == ([0-9]{1,5}) \]\]", log.replace("\\", ""))
    return {"signals": [sig for sig in signals if sig.casefold() in log.casefold()],
            "source_locations": sorted(set(f"{file}:{line}" for file, line in locations))[:12],
            "last_operation": max(stages)[1] if stages else None,
            "last_compared_mtus": [int(x) for x in checks[-1]] if checks else []}


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stage_release(name, source):
    if STAGED.exists():
        assert not STAGED.is_symlink() and {p.relative_to(STAGED).as_posix() for p in STAGED.rglob("*") if p.is_file()} == RELEASE_FILES
        shutil.rmtree(STAGED)
    git("checkout", "--quiet", "--detach", source)
    assert git("rev-parse", "HEAD") == source and not git("status", "--porcelain", "--untracked-files=no")
    private = ROOT / "veth-private" / (name + "-release")
    private.mkdir()
    raw = private / "build.jsonl"
    row = {"source": source, "target": "//:release", "requested_spawn_strategy": "remote", "remote_accept_cached": True}
    result.setdefault("source_builds", {})[name] = row
    save()
    with (private / "console").open("wb") as stream:
        row["exit"] = subprocess.run(["docker", "exec", "bridge-gnu", "timeout", "--signal=INT", "--kill-after=30s",
            "1800", "bazel", "--batch", f"--bazelrc={ROOT / 'bridge.bazelrc'}", "build", "--config=x86_64",
            "--strip=never", "--lockfile_mode=off", "--remote_download_outputs=toplevel",
            f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", "//:release"],
            stdout=stream, stderr=subprocess.STDOUT).returncode
    row["command_boundary"] = boundary((private / "console").read_text(errors="replace"))
    save()
    for line in raw.open():
        event = json.loads(line)
        if "started" in event:
            identifier = event["started"]["uuid"]
            assert re.fullmatch(r"[a-f0-9-]{36}", identifier)
            row["invocation"] = identifier
        if "buildMetrics" in event:
            runners = event["buildMetrics"].get("actionSummary", {}).get("runnerCount", [])
            row["runners"] = {v["name"]: v.get("count", 0) for v in runners if v.get("name") in
                              ("remote", "remote cache hit", "internal", "local", "linux-sandbox", "worker")}
    save()
    assert row["exit"] == 0 and "invocation" in row and not git("status", "--porcelain", "--untracked-files=no")
    release = Path("bazel-bin/release")
    produced = {p.relative_to(release).as_posix() for p in release.rglob("*") if p.is_file()}
    assert produced == RELEASE_FILES
    row["binaries"] = {rel: checksum(release / rel) for rel in sorted(produced)}
    with (release / "runsc").open("rb") as runtime:
        assert runtime.read(4) == b"\x7fELF"
    shutil.copytree(release, STAGED)
    assert all(os.access(STAGED / rel, os.X_OK) and checksum(STAGED / rel) == value for rel, value in row["binaries"].items())
    save()



def execute(name, source, target):
    if name != "nogo":
        stage_release(name, source)
        source = CARRIER
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
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=360",
            "--remote_download_outputs=toplevel", "--test_output=errors",
            f"--test_env=VETH_MTU_QUALIFICATION={os.environ['GITHUB_RUN_ID']}-{name}",
            "--strategy=TestRunner=" + ("remote" if name == "nogo" else "local"),
            f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", target]
    if name == "nogo":
        command = ["docker", "exec", "bridge-gnu", "timeout", "--signal=INT", "--kill-after=30s", "1800",
                   "bazel", "--batch", f"--bazelrc={ROOT / 'bridge.bazelrc'}", *args]
    else:
        args = [*args[:-1], "--remote_cache=", "--remote_executor=", "--bes_backend=",
                "--build_tag_filters=", "--test_tag_filters=", target]
        command = ["env", "-i", "HOME=" + os.environ["HOME"], "USER=" + os.environ["USER"],
                   "PATH=" + os.environ["PATH"], "timeout", "--signal=INT", "--kill-after=30s", "780",
                   shutil.which("bazel"), "--batch", *args]
    (out / "executed_arguments").write_text("\n".join(args) + "\n")
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
    console = (out / "console").read_text(errors="replace")
    progress = re.findall(r"\[\s*([0-9,]+)\s*/\s*([0-9,]+)\s*\]", console)
    row["last_action_count"] = [int(v.replace(",", "")) for v in progress[-1]] if progress else []
    row["timeout"] = code == 124
    row["command_boundary"] = boundary(console)
    save()
    captured = report.capture(raw, events, Path("bazel-out"), out / "reports")
    row["reports"] = [{key: item[key] for key in ("name", "sha256", "bytes")} for item in captured["reports"]]
    logs = [out / item["path"] for item in captured["reports"] if item["name"] == "test.log"]
    log = logs[0].read_text(errors="replace") if len(logs) == 1 else ""
    row["test_boundary"] = boundary(log)
    if name != "nogo":
        row["launched_binaries"] = dict(re.findall(r"(?m)^VETH_BINARY (runsc|gvisor_sentry) ([a-f0-9]{64})$", log))

    readings = {field: re.findall(rf"\+ {field}=([0-9]{{1,5}})\b", log) for field in FIELDS}
    row["mtu_readings"] = {field: int(values[-1]) for field, values in readings.items() if values}
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
    save()
    assert git("rev-parse", "HEAD") == source
    assert not (out / "source_before").read_text().strip() and not (out / "source_after").read_text().strip()
    assert info.get("strategy") == ("remote" if name == "nogo" else "local")
    assert not observation.get("cachedLocally") and not info.get("cachedRemotely")
    if name == "nogo":
        row["architecture_markers"] = [arch for arch in ("amd64", "arm64") if arch + "..." in log]
        assert code == 0 and observation["status"] == "PASSED" and row["architecture_markers"]
        row["status"] = "PASS"
    else:
        binaries = result["source_builds"][name]["binaries"]
        assert row["launched_binaries"] == {"runsc": binaries["runsc"], "gvisor_sentry": binaries["gvisor-bin/gvisor_sentry"]}
        assert all(len(set(values)) == 1 for values in readings.values())
        assert [row["mtu_readings"].get(field) for field in FIELDS] == EXPECTED[name]
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
    assert git("rev-parse", "HEAD^") == "189e0ceeac82ef76744c0820e9718cb218846875"
    assert git("rev-parse", PUBLISHED + "^{tree}") == git("rev-parse", PRODUCT + "^{tree}")
    result["published"] = PUBLISHED
    result["tree"] = git("rev-parse", PUBLISHED + "^{tree}")
    result["coordinator"] = {"kernel": subprocess.check_output(["uname", "-r"], text=True).strip(),
                             "architecture": subprocess.check_output(["uname", "-m"], text=True).strip(),
                             "fixture": OCI, "platform": "systrap", "network": "none"}
    assert result["coordinator"]["architecture"] == "x86_64"
    prior_native = (ROOT / "veth-native.zip").read_bytes()
    assert hashlib.sha256(prior_native).hexdigest() == "31ce6563257c04cfcce143862989f17dd50056f78cae5d637ec2d44c0db2dfc9"
    with zipfile.ZipFile(io.BytesIO(prior_native)) as archive:
        native = json.loads(archive.read("native-veth-mtu.json"))
    assert native["status"] == "PASS" and [r["mtus"] for r in native["reads"]] == [[1500,1500],[3000,1500],[3000,1460]]
    result["previous_native"] = {"run": 41730911731, "artifact": 13277765046, "row": native}
    script = "test/rtnetlink/linux/setlink_test.sh"
    assert git("rev-parse", CARRIER + ":" + script) == git("rev-parse", STOCK + ":" + script) == git("rev-parse", PUBLISHED + ":" + script)
    result["setlink_blob"] = git("rev-parse", CARRIER + ":" + script)
    original = (ROOT / "veth-original.zip").read_bytes()
    assert hashlib.sha256(original).hexdigest() == "0d45db8f8b717b2435c89ff3e3b4e20517014d5386dd5e7dff7f000f43c07e83"
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        old = json.loads(archive.read("verdict.json"))
    unit = old["arms"]["unit"]
    assert unit["source"] == PRODUCT and unit["status"] == "PASS" and unit["mtu_test_passed"]
    result["previous_unit"] = {"run": 41732386526, "artifact": 13277699861, "row": unit}
    save()
    assert git("rev-parse", STOCK + "^") == PRODUCT
    assert git("rev-parse", PRODUCT + "^") == BASE
    endpoint = "pkg/tcpip/link/veth/veth.go"
    assert git("diff", "--name-only", STOCK, PRODUCT) == endpoint
    assert git("rev-parse", STOCK + ":" + endpoint) == git("rev-parse", BASE + ":" + endpoint)
    assert git("diff", "--name-only", BASE, PRODUCT).splitlines() == [
        "pkg/tcpip/link/veth/BUILD", endpoint, "pkg/tcpip/link/veth/veth_test.go", "test/rtnetlink/linux/setlink_test.sh"]
    for name, source, target in (("stock", STOCK, OCI), ("fixed", PUBLISHED, OCI), ("nogo", PUBLISHED, NOGO)):
        if name == "fixed" and not result["arms"]["stock"].get("mtu_readings"):
            result["arms"]["fixed"] = {"status": "INCOMPLETE", "reason": "stock did not reach guest MTU reads"}
            continue
        try:
            execute(name, source, target)
        except Exception as error:
            result["arms"].setdefault(name, {"status": "INCOMPLETE"})["verifier_error"] = type(error).__name__
            save()
    statuses = {name: row["status"] for name, row in result["arms"].items()}
    result["status"] = "PASS" if statuses == {"stock": "EXPECTED_FAILURE", "fixed": "PASS", "nogo": "PASS"} else (
        "FAIL" if "FAIL" in statuses.values() else "INCOMPLETE")
finally:
    save()
assert result["status"] == "PASS"
