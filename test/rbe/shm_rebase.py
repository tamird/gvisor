"""Verify the registered SHM syscall tests and the package's split Nogo wrapper."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import traceback
import xml.etree.ElementTree as ET

import qualification_report as report


ROOT = Path(os.environ["RUNNER_TEMP"])
PRODUCT = "90a37644f7c3e99f4c21954dc9e4181a494094ec"
BASE = "007bd6c1a9f79faf15bb32daaff382ec1af5ab63"
FILES = {"pkg/sentry/kernel/shm/shm.go": "e846ad5d85ccb8f7939aae59d8e94d35a617e68a",
         "test/syscalls/linux/shm.cc": "ee66b8d9b5546ef1888790293c5bc97f109e4e66"}
TARGETS = {"native": "//test/syscalls:shm_test_native",
           "systrap": "//test/syscalls:shm_test_runsc_systrap_directfs",
           "nogo": "//pkg/sentry/kernel/shm:shm_nogo"}
CASES = ("RemovedSegmentsAreMarkedDeleted", "RemovedSegmentsAreDestroyed",
         "AllowsAttachToRemovedSegmentWithRefs", "RemovedSegmentsAreNotDiscoverable",
         "NoDestructionOfAttachedSegmentWithMultipleRmid", "RmidOwnershipPermissionDenied")


def utc():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(["git", *args])


def save():
    result["verified_at"] = utc()
    (ROOT / "out" / "verdict.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")


def execute(arm, target, row):
    out = ROOT / "private" / arm
    out.mkdir(parents=True)
    raw = out / "raw.jsonl"
    (out / "commit_file").write_bytes(git("cat-file", "commit", "HEAD"))
    (out / "source_before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (out / "targets").write_text(target + "\n")
    placement = "local" if arm == "native" else "linux-sandbox"
    args = ["test", "--config=x86_64", "--lockfile_mode=off", "--jobs=4",
            "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
            "--strategy=TestRunner=" + placement, "--remote_executor=", "--remote_cache=", "--bes_backend=",
            "--nocache_test_results", "--runs_per_test=1", "--flaky_test_attempts=1",
            "--sandbox_add_mount_pair=/tmp", "--test_sharding_strategy=disabled", "--test_timeout=180",
            "--test_output=errors", "--build_event_json_file=" + str(raw), "--build_event_json_file_path_conversion=false"]
    if arm != "nogo":
        args.append("--test_env=GTEST_FILTER=" + ":".join("ShmTest." + case for case in CASES))
    args.append(target)
    (out / "executed_arguments").write_text("\n".join(args) + "\n")
    command = ["timeout", "--signal=INT", "--kill-after=30s", "1500", "bazel", "--batch",
               "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", *args]
    if arm == "native":
        # NET_ADMIN would make test/runner enter a user namespace mapping only
        # ID zero. Keep the worker's distinct, mapped scratch UID and GID.
        prefix = ["setpriv", "--bounding-set=-net_admin,-sys_admin", "--"]
        status = subprocess.check_output([*prefix, "cat", "/proc/self/status"], text=True)
        caps = int(re.search(r"^CapEff:\s*([0-9a-f]+)$", status, re.M)[1], 16)
        row["credential_preconditions"] = {
            "set_ids": (caps & 0xc0) == 0xc0, "without_net_admin": not caps & (1 << 12),
            "without_sys_admin": not caps & (1 << 21)}
        for kind in ("uid", "gid"):
            ranges = [list(map(int, line.split())) for line in Path("/proc/self/" + kind + "_map").read_text().splitlines()]
            row["credential_preconditions"][kind + "_65534_mapped"] = any(start <= 65534 < start + size for start, _, size in ranges)
        save()
        assert all(row["credential_preconditions"].values())
        command = [*prefix, *command]
    with (out / "console").open("wb") as stream:
        code = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT).returncode
    finished = utc()
    row.update(native_exit=code, execution_finished_at=finished, status="FAIL" if code else "INCOMPLETE")
    save()
    console = (out / "console").read_bytes()
    row["console_sha256"] = hashlib.sha256(console).hexdigest()
    row["diagnostic_classes"] = [term for term in (
        "Error downloading", "No space left on device", "no such package", "Permission denied", "not found",
        "undefined reference", "not registered", "must hold", "checklocks:", "GoStandardLibraryAnalysis",
        "Invalid argument", "Operation not permitted", "setting up network", "device or resource busy")
        if term.lower().encode() in console.lower()]
    # Include the first bounded file/line, errno or link reason when available.
    def references(text):
        return sorted(set(re.findall(r"(?:shm\.(?:go|cc):\d+(?::\d+)?|vdso\.cc:\d+|ld\.lld: error: undefined symbol: [A-Za-z_]\w*|\b(?:EPERM|EINVAL|EBUSY|EACCES)\b)", text)))[:16]
    row["diagnostic_locations"] = references(console.decode(errors="replace"))
    save()
    (out / "native_exit").write_text(str(code))
    (out / "source_after").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (out / "final_head").write_bytes(git("rev-parse", "HEAD"))
    clean = out / "events.jsonl"
    manifest = report.capture(raw, clean, Path("bazel-out"), out / "reports")
    (out / "capture_exit").write_text(str(int(bool(manifest["errors"]))))
    row["reports"] = [{k: item[k] for k in ("name", "sha256", "bytes", "identity")} for item in manifest["reports"]]
    save()
    started, attempts = [], []
    with clean.open() as stream:
        for line in stream:
            event = json.loads(line)
            if "started" in event:
                started.append(event["started"].get("uuid", ""))
            if "testResult" in event:
                attempt = event["testResult"]
                info = attempt.get("executionInfo", {})
                attempts.append({"identity": event["id"]["testResult"], "status": attempt["status"],
                                 "strategy": info.get("strategy"), "cached_locally": attempt.get("cachedLocally", False),
                                 "cached_remotely": info.get("cachedRemotely", False)})
    row.update(test_attempts=len(attempts), attempts=attempts)
    save()
    files = {item["name"]: out / item["path"] for item in manifest["reports"]}
    assert set(files) == {"test.log", "test.xml"}
    text = files["test.log"].read_text(errors="replace")
    row["test_diagnostic_locations"] = references(text)
    nodes = list(ET.parse(files["test.xml"]).iter("testcase"))
    observations = []
    for node in nodes:
        name = node.get("name")
        known = (node.get("classname") == "ShmTest" and name in CASES) or (arm == "nogo" and name == target[2:].replace(":", "/"))
        state = next((child.tag for child in node if child.tag in ("failure", "error", "skipped")), "pass")
        if node.get("status") == "notrun" or node.get("result") == "suppressed":
            state = "skipped"
        observations.append({"name": name if known else "<unexpected>", "status": state})
    row["xml_cases"] = observations
    row["xml_case_counts"] = {state: sum(case["status"] == state for case in observations)
                              for state in ("pass", "failure", "error", "skipped")}
    if arm == "nogo":
        row["architectures"] = re.findall(r"^(amd64|arm64)\.\.\.PASS\s*$", text, re.M)
        expected = 1
    else:
        expected = len(CASES)
    save()
    if len(started) == 1 and re.fullmatch(r"[a-f0-9-]{36}", started[0]):
        row["invocation"] = started[0]
    keys = ("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit")
    context = {key: key for key in keys}
    context.update(expected_source=PRODUCT, command_kind="test", execution_finished_at=finished)
    report.write_json(out / "context", context)
    verdict = report.verdict(clean, report.manifest_path(clean), out / "context", out / "targets", ())
    row.update(original_status=verdict["status"], case_counts=verdict["case_counts"])
    save()
    attempt, = attempts
    identity = attempt["identity"]
    assert len(verdict["executed"]) == 1 and not (verdict["errors"] or verdict["missing"] or manifest["errors"])
    assert identity["label"] == target and re.fullmatch(r"[0-9a-f]{64}", identity["configuration"]["id"])
    assert all(identity.get(k, 1) == 1 for k in ("run", "shard", "attempt"))
    assert all(item["identity"] == identity for item in manifest["reports"])
    assert attempt["strategy"] == placement and not attempt["cached_locally"] and not attempt["cached_remotely"]
    if arm == "nogo":
        assert sorted(row["architectures"]) == ["amd64", "arm64"]
    else:
        assert {case["name"] for case in observations} == set(CASES)
    assert len(nodes) == expected and verdict["case_counts"] == {"passed": expected, "failed": 0, "skipped": 0}
    assert all(case["name"] != "<unexpected>" and case["status"] == "pass" for case in observations)
    assert code == 0 and verdict["status"] == "PASS" and attempt["status"] == "PASSED"
    row["status"] = "PASS"


result = dict(product=PRODUCT, base=BASE, original_reconciliation="1e2c3bce4cbcd408e303e322cbe97fc11caa585d",
              previous_artifacts=[13119345175, 13119595225, 13256418500], expected=list(TARGETS),
              original_projection={"artifact": 13355442664, "status": "INCOMPLETE", "native_exits": [0, 0, 0],
                                   "zip_sha256": "5ad6ec151d7681f7783d96bbefd77f05a0877c152309ec1234cb73abb4db4cf4"},
              arms={}, status="INCOMPLETE", started_at=utc(), kernel=platform.release(), machine=platform.machine())
save()
assert platform.machine() == "x86_64" and os.getuid() == 0
assert git("rev-parse", "HEAD").decode().strip() == PRODUCT and git("rev-parse", "HEAD^").decode().strip() == BASE
assert git("diff", "--name-only", BASE, PRODUCT).decode().splitlines() == sorted(FILES)
assert all(git("rev-parse", "HEAD:" + path).decode().strip() == blob for path, blob in FILES.items())
for arm, target in TARGETS.items():
    row = result["arms"][arm] = {"target": target, "status": "INCOMPLETE", "started_at": utc()}
    try:
        execute(arm, target, row)
    except Exception as error:
        row["verifier_error"] = type(error).__name__
        row["verifier_line"] = traceback.extract_tb(error.__traceback__)[-1].lineno
    save()
statuses = [row["status"] for row in result["arms"].values()]
result["failed"] = [arm for arm, row in result["arms"].items() if row["status"] == "FAIL"]
result["missing"] = [arm for arm, row in result["arms"].items() if row["status"] == "INCOMPLETE"]
result["status"] = "PASS" if all(s == "PASS" for s in statuses) else "FAIL" if result["failed"] else "INCOMPLETE"
save()
raise SystemExit(int(result["status"] != "PASS"))
