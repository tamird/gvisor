"""Retain original dispositions and qualify only the missing runtime owners."""

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

import qualification_report as reporting

ROOT = Path(os.environ["RUNNER_TEMP"])
PUBLIC = ROOT / "replay-public"
ORIGINAL_SHA = "2d6b29c0171342081d34d9e31eb20fda3d1fb572f895ca79e71b36b66fdb395e"
LOG_SHA = "9735bbe867f6937c40fefa8e8b4ed38162d17e2dc1e8c83af9aa3bca9fd90514"
SHM = "1e2c3bce4cbcd408e303e322cbe97fc11caa585d"
ARM_INVOCATION = "473c2294-c93f-41af-8f64-070afd860619"
ARM_OWNER = "//pkg/sentry/kernel/shm:shm_nogo"
ARMS = ("shm-arm64", "vfs-tests", "vfs-amd64", "vfs-arm64", "shm-native-ids")
SECRETS = [os.environ[k] for k in ("GH_TOKEN", "BUILDBUDDY_API_KEY") if os.environ.get(k)]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


def save(name, value):
    data = json.dumps(value, sort_keys=True, indent=2) + "\n"
    assert len(data.encode()) < 2 * 1024**2
    assert not any(s in data or json.dumps(s)[1:-1] in data for s in SECRETS)
    assert not re.search(r"(?i)(?:authorization|x-buildbuddy-api-key)\s*[:=]|github_pat_|gh[pousr]_", data)
    (PUBLIC / name).write_text(data)


def original():
    from cas_inputs import fetch, rpc

    raw = subprocess.check_output(["gh", "api", "repos/tamird/gvisor/actions/artifacts/13256418500/zip"])
    assert sha(raw) == ORIGINAL_SHA
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        expected = {"summary.json"} | {a + s for a in ("race", "pgalloc-analysis", "shm-amd64", "shm-syscalls")
                                     for s in (".json", "-native-exit.json", "-runner-exit.json")}
        assert set(archive.namelist()) == expected
        prior = {name: json.loads(archive.read(name)) for name in expected}
    assert prior["shm-syscalls.json"]["source"] == SHM
    assert prior["shm-syscalls.json"]["native_exit"] == 3
    assert prior["shm-syscalls.json"]["case_counts"] == {"passed": 11, "failed": 1, "skipped": 0}
    assert prior["shm-amd64.json"]["verified"]
    for name, value in prior.items():
        save("original-" + name, value)
    log = subprocess.check_output(["gh", "api", "--allow-escape-sequences",
                                   "repos/tamird/gvisor/actions/jobs/126061989661/logs"])
    assert sha(log) == LOG_SHA
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", log.decode())
    assert ARM_INVOCATION in text
    diagnostics = ("test/syscalls/linux/shm.cc:577: Failure", "test/syscalls/linux/shm.cc:578: Failure",
                   "syscall(119, -1, scratch_gid, -1)", "syscall(117, -1, scratch_uid, -1)",
                   "PosixError(errno=22 Invalid argument)", "Actual: No Error, has a value 0")
    assert all(s in text for s in diagnostics)
    result = dict(original_zip_sha256=sha(raw), original_log_sha256=sha(log),
                  original_log_bytes=len(log), original_source="d46ece07b13efa292e3f196fada41725392de5bc",
                  original_native_failure=dict(owner="//test/syscalls:shm_test_native",
                    case="ShmTest/RmidOwnershipPermissionDenied", native_exit=3,
                    first_failures=[dict(line=577, syscall="setresgid", errno=22),
                                    dict(line=578, syscall="setresuid", errno=22)],
                    subsequent_ipc_rmid=0, scratch_uid=65534, scratch_gid=65534,
                    capability_gate_skipped=False,
                    original_namespace_mapping="not recorded; single-ID runner map is a source explanation"),
                  arm64=dict(invocation=ARM_INVOCATION, owner=ARM_OWNER,
                    last_console_progress="2400/2430", last_console_at="2026-11-03T18:11:32.902491Z"),
                  workload_repeated=False, status="ORIGINAL_RECORDED", started=utc())
    save("recovery.json", result)
    # Read the selected invocation before deciding whether its unfinished owner needs execution.
    arm = result["arm64"]
    try:
        response = rpc("GetInvocation", {"lookup": {"invocationId": ARM_INVOCATION}})
        invocation, = response["invocation"]
        assert invocation["invocationId"] == ARM_INVOCATION
        arm["invocation_metadata_sha256"] = sha(json.dumps(response, sort_keys=True).encode())
        state = invocation.get("invocationStatus")
        assert state in (None, 0, 1, 2, 3, 4, "UNKNOWN_INVOCATION_STATUS", "COMPLETE_INVOCATION_STATUS",
                         "PARTIAL_INVOCATION_STATUS", "DISCONNECTED_INVOCATION_STATUS", "MISSING_API_KEY_INVOCATION_STATUS")
        arm["invocation_status"] = state
        assert invocation.get("success") in (None, True, False)
        arm["success"] = invocation.get("success")
        # Never serialize arbitrary metadata or console fields.
        code = invocation.get("bazelExitCode")
        assert code is None or isinstance(code, str) and re.fullmatch(r"[A-Z_]{0,64}", code)
        arm["bazel_exit_code"] = code
        save("recovery.json", result)
        response = json.loads(fetch("api/v1/GetAction", {"selector": {
            "invocation_id": ARM_INVOCATION, "target_label": ARM_OWNER}}))
        assert not response.get("next_page_token", response.get("nextPageToken"))
        actions = response.get("action", [])
        assert len(actions) <= 8
        attempts = []
        for action in actions:
            assert action.get("target_label", action.get("targetLabel")) == ARM_OWNER
            identity = action["id"]
            assert identity.get("invocation_id", identity.get("invocationId")) == ARM_INVOCATION
            if any(int(action.get(k, 0)) for k in ("run", "shard", "attempt")):
                config = identity.get("configuration_id", identity.get("configurationId"))
                assert re.fullmatch(r"[a-f0-9]{64}", config)
                attempts.append(dict(configuration=config, **{k: int(action.get(k, 0)) for k in ("run", "shard", "attempt")}))
        arm.update(action_metadata_sha256=sha(json.dumps(response, sort_keys=True).encode()),
                   test_attempts=attempts, lookup_complete=True)
        # Unexpected retained attempts need evidence reconciliation, never an automatic repeat.
        assert not attempts, "Original ARM64 test attempt exists; reconcile before executing"
        assert state in (1, 3, "COMPLETE_INVOCATION_STATUS", "DISCONNECTED_INVOCATION_STATUS"), "Original invocation is not terminal"
        arm["missing_test_confirmed"] = True
    except Exception as error:
        arm["lookup_error"] = type(error).__name__
        result["status"] = "INCOMPLETE"
    result["finished"] = utc()
    save("recovery.json", result)


def _complete():
    rows = {a: json.loads((PUBLIC / (a + ".json")).read_text()) for a in ARMS if (PUBLIC / (a + ".json")).is_file()}
    result = dict(expected=list(ARMS), executed=list(rows), missing=sorted(set(ARMS) - rows.keys()),
                  original_zip_sha256=ORIGINAL_SHA, original_shm_syscall_verdict="FAIL", verdict_available_at=utc())
    errors = []
    if "shm-native-ids" in rows:
        row = rows["shm-native-ids"]
        private = ROOT / "replay-private/shm-native-ids"
        manifest = json.loads(reporting.manifest_path(private / "events.jsonl").read_text())
        logs = "\n".join((private / f["path"]).read_text() for f in manifest["reports"] if f["name"] == "test.log")
        maps = {key: [list(map(int, m)) for m in re.findall(r"^" + key + r" (\d+) (\d+) (\d+)$", logs, re.M)]
                for key in ("UID_MAP", "GID_MAP")}
        capabilities = re.findall(r"^CAP_EFF ([0-9a-fA-F]{16})$", logs, re.M)
        binaries = re.findall(r"^SHM_ELF ([a-f0-9]{64})$", logs, re.M)
        proof = dict(maps=maps, cap_eff=capabilities, elf_sha256=binaries,
                     uid_zero=bool(re.search(r"^UID 0$", logs, re.M)),
                     architecture=bool(re.search(r"^ARCH x86_64$", logs, re.M)))
        cases = []
        for f in manifest["reports"]:
            if f["name"] == "test.xml":
                import xml.etree.ElementTree as ET
                cases.extend((c.get("classname"), c.get("name")) for c in ET.parse(private / f["path"]).getroot().iter("testcase"))
        if row["case_counts"] != {"passed": 1, "failed": 0, "skipped": 0} or cases != [("ShmTest", "RmidOwnershipPermissionDenied")]:
            errors.append("native ownership case did not execute and pass exactly once")
        if not proof["uid_zero"] or not proof["architecture"] or len(capabilities) != 1 or len(binaries) != 1:
            errors.append("native identity proof absent")
        elif int(capabilities[0], 16) & ((1 << 6) | (1 << 7)) != (1 << 6) | (1 << 7):
            errors.append("native SETUID/SETGID unavailable")
        if any(not any(start <= 65534 < start + count for start, _, count in value) for value in maps.values()):
            errors.append("native scratch IDs unmapped")
        result["native_identity"] = proof
    result["verification_errors"] = errors
    native_failures = {a: json.loads((PUBLIC / (a + "-native-exit.json")).read_text()) for a in ARMS
                       if (PUBLIC / (a + "-native-exit.json")).is_file()}
    result["native_exits"] = native_failures
    result["status"] = "FAIL" if any(code == 3 for code in native_failures.values()) or errors else (
        "PASS" if not result["missing"] and all(v["verified"] for v in rows.values()) else "INCOMPLETE")
    return result


def complete():
    try:
        result = _complete()
    except Exception as error:
        exits = {a: json.loads((PUBLIC / (a + "-native-exit.json")).read_text()) for a in ARMS
                 if (PUBLIC / (a + "-native-exit.json")).is_file()}
        result = dict(status="FAIL" if 3 in exits.values() else "INCOMPLETE", native_exits=exits,
                      verifier_error=type(error).__name__, original_zip_sha256=ORIGINAL_SHA,
                      original_shm_syscall_verdict="FAIL", verdict_available_at=utc())
    result["qualification_source"] = os.environ["GITHUB_SHA"]
    save("summary.json", result)
    assert result["status"] == "PASS"


if __name__ == "__main__":
    if sys.argv[1] == "original":
        original()
    elif sys.argv[1] == "complete":
        complete()
    else:
        assert sys.argv[1] == "scan"
        for path in PUBLIC.glob("*.json"):
            save(path.name, json.loads(path.read_text()))
