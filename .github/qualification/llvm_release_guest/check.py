"""Check one original guest transformation and its complete release on each native CPU."""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET


SOURCE = "4bee07965270dc7cb4ed89eb6a3b17968b3e0a90"
PACKAGE = "examples/sandboxexec/go/docprocessor"
TARGET = f"//{PACKAGE}:docprocessor_test"
CASE = "TestProcessDocuments_BasicTransform"
IMAGE = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
ARCHES = {"amd64": ("x86_64", "x86_64", 62), "arm64": ("aarch64", "aarch64", 183)}
TASK = Path(os.environ["RUNNER_TEMP"]) / "llvm-release-guest"
FILES = ["runsc", "containerd-shim-runsc-v1"] + [
    "gvisor-bin/" + name for name in (
        "gvisor_sentry", "checkpointgofer", "gvisor-sentry-prewarmer",
        "runsc-fd-parking", "runsc-metric-server", "runsc-netgofer", "gvisor-proc-umounter",
    )
]
HEX = r"[0-9a-f]{64}"


def save(name, row):
    (TASK / "results" / (name + ".json")).write_text(json.dumps(row, indent=2) + "\n")


def observe():
    return """set -eu
printf 'NATIVE_RELEASE_WORKER %s %d\\n' "$(uname -m)" "$EUID"
cd_root="$TEST_SRCDIR/$TEST_WORKSPACE/release"
for member in """ + shlex.join(FILES) + """; do
    file="$cd_root/$member"
    test -x "$file"
    head=$(od -An -tx1 -N6 "$file" | tr -d ' \\n')
    machine=$(od -An -tu2 -j18 -N2 "$file" | tr -d ' \\n')
    sha=$(sha256sum "$file")
    printf 'RELEASE_ELF %s %s %s %s\\n' "$member" "$head" "$machine" "${sha%% *}"
done
unset GVISOR_SIDECAR_BINARIES_DIR
export RUNSC_PATH=/nonexistent/qualification-missing-packaged-runsc
exec "$@"
"""


def parse_files(result, row, arch):
    directory = Path("bazel-testlogs") / PACKAGE / "docprocessor_test"
    for item in result.get("testActionOutput", []):
        name = item["name"]
        if name not in {"test.xml", "test.log"}:
            continue
        data = (directory / name).read_bytes()
        assert len(data) <= 2 * 1024 * 1024
        digest = hashlib.sha256(data).hexdigest()
        cas = re.search(r"/(?:blobs|compressed-blobs/zstd)/(" + HEX + r")/(\d+)$", item.get("uri", ""))
        assert (cas[1] == digest and int(cas[2]) == len(data)) if cas else item.get("digest") == digest
        row[name] = digest
        if name == "test.xml":
            cases = list(ET.fromstring(data).iter("testcase"))
            row["xml_cases"] = [
                ["selected" if node.get("name", "").split("/")[-1].split(".")[-1] == CASE else "other",
                 "FAIL" if any(node.find(k) is not None for k in ("failure", "error")) else
                 "SKIP" if node.find("skipped") is not None or node.get("status") == "notrun" else "PASS"]
                for node in cases
            ][:10]
            row["xml_count"] = len(cases)
            continue
        log = data.decode(errors="replace")
        markers = re.findall(r"(?m)^NATIVE_RELEASE_WORKER (x86_64|aarch64) ([0-9]+)$", log)
        row["native_worker"] = markers == [(ARCHES[arch][1], "0")]
        row["observed_uname"] = markers[0][0] if len(markers) == 1 else None
        row["go_pass"] = bool(re.search(r"(?m)^\x16?--- PASS: " + CASE + r" \(", log))
        row["release"] = {}
        for member, magic, machine, sha in re.findall(r"(?m)^RELEASE_ELF ([\w/-]+) ([a-f0-9]{12}) ([0-9]+) (" + HEX + r")$", log):
            assert member in FILES and member not in row["release"]
            row["release"][member] = {"elf64_le": magic == "7f454c460201", "machine": int(machine), "sha256": sha}
        row["diagnostics"] = [message for message in (
            "failed to create sandbox", "execution timed out", "command exited with code",
            "failed to read output file", "operation not permitted", "permission denied",
            "exec format error", "No such file or directory", "not found",
        ) if message in log]


def actual(arch, row):
    config = ARCHES[arch][0]
    platform = "//tools/bazeldefs:linux_" + arch
    bep = TASK / (arch + ".bep")
    log = TASK / (arch + ".log")
    command = [
        str(TASK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx8g",
        "--bazelrc=" + str(TASK / "remote.rc"), "test", "--config=" + config,
        "--host_platform=" + platform, "--extra_execution_platforms=" + platform,
        "--remote_default_exec_properties=Arch=" + arch, "--remote_default_exec_properties=OSFamily=linux",
        "--remote_default_exec_properties=container-image=" + IMAGE,
        "--remote_default_exec_properties=workload-isolation-type=oci",
        "--spawn_strategy=remote", "--strategy=TestRunner=remote", "--noremote_local_fallback",
        "--jobs=50", "--remote_download_outputs=minimal", "--build_tests_only", "--nocache_test_results",
        "--flaky_test_attempts=1", "--test_sharding_strategy=disabled", "--test_timeout=120",
        "--modify_execution_info=^TestRunner$=+no-remote-cache",
        "--test_filter=^" + CASE + "$", "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1",
        "--run_under=bash -c " + shlex.quote(observe()) + " inspect-release",
        "--remote_download_regex=.*testlogs/" + PACKAGE + "/docprocessor_test/test[.](log|xml)",
        "--build_event_json_file=" + str(bep), "--color=no", "--curses=no", "--noshow_progress", TARGET,
    ]
    with log.open("wb") as out:
        try:
            row["exit"] = subprocess.run(command, stdout=out, stderr=subprocess.STDOUT, timeout=3300).returncode
        except subprocess.TimeoutExpired:
            row["outer_deadline"] = True
    for line in bep.open():
        event = json.loads(line)
        if "started" in event:
            row["invocation"] = str(uuid.UUID(event["started"]["uuid"]))
        if "finished" in event:
            row["bazel_exit"] = event["finished"]["exitCode"]["name"]
        if "action" in event and not event["action"].get("success"):
            failed = event["action"].get("type", "")
            if re.fullmatch(r"[A-Za-z0-9_]{1,60}", failed) and failed not in row["failed_mnemonics"]:
                row["failed_mnemonics"] = (row["failed_mnemonics"] + [failed])[:5]
        if "testResult" not in event:
            continue
        assert event["id"]["testResult"]["label"] == TARGET
        result = event["testResult"]
        info = result.get("executionInfo", {})
        attempt = {"status": result["status"], "remote": info.get("strategy") == "remote",
                   "cached": bool(info.get("cachedRemotely") or result.get("cachedLocally"))}
        row["attempts"].append(attempt)
        parse_files(result, attempt, arch)
    if len(row["attempts"]) != 1:
        return False
    a = row["attempts"][0]
    release = a.get("release", {})
    return (row.get("exit") == 0 and a["status"] == "PASSED" and a["remote"] and not a["cached"]
            and a.get("native_worker") and a.get("go_pass") and a.get("xml_count") == 1
            and a.get("xml_cases") == [["selected", "PASS"]] and set(release) == set(FILES)
            and all(x["elf64_le"] and x["machine"] == ARCHES[arch][2] for x in release.values()))


def worker(arch, row):
    sys.path.insert(0, str(TASK))
    import exec_cas as cas

    source = json.loads((TASK / "results" / ("actual-" + arch + ".json")).read_text())
    assert source["source"] == SOURCE and source["target"] == TARGET
    cas.INV = str(uuid.UUID(source["invocation"]))
    row["invocation"] = cas.INV
    for execution in cas.executions(TARGET, "TestRunner"):
        rep = execution.get("executeResponse", {})
        result = rep.get("result")
        if result is None:
            continue
        action = dict(cas.fields(cas.blob(execution["actionDigest"], 128 * 1024)))
        cmd = list(cas.fields(cas.blob(cas.digest_msg(action[1]), 4 * 1024 * 1024)))
        args = [s.decode() for t, s in cmd if t == 1]
        if not (any(s.endswith("/test-setup.sh") for s in args) or any(
                f.get("path", "").endswith("/test.cache_status") for f in result.get("outputFiles", []))):
            continue
        platform = action.get(10) or next((s for t, s in cmd if t == 5), b"")
        props = {}
        for tag, value in cas.fields(platform):
            if tag == 1:
                pair = dict(cas.fields(value))
                props[pair[1].decode()] = pair[2].decode()
        row["actions"].append({
            "action": execution["actionDigest"]["hash"], "expected_cpu": props.get("Arch") == arch,
            "linux": props.get("OSFamily") == "linux", "ubuntu": props.get("container-image") == IMAGE,
            "firecracker": props.get("workload-isolation-type") == "firecracker",
            "root": props.get("dockerUser") == "root", "ipv6": props.get("network-enable-ipv6") == "true",
            "qemu_command": any("qemu-" in s for s in args), "cached": bool(rep.get("cachedResult")),
            "exit": result.get("exitCode", 0), "service_code": rep.get("status", {}).get("code", 0),
        })
    if len(row["actions"]) != 1:
        return False
    a = row["actions"][0]
    return (all(a[k] for k in ("expected_cpu", "linux", "ubuntu", "firecracker", "root", "ipv6"))
            and not a["qemu_command"] and not a["cached"] and a["exit"] == a["service_code"] == 0)


if __name__ == "__main__":
    phase, arch = sys.argv[1:]
    assert arch in ARCHES and phase in ("actual", "worker")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    row = {"source": SOURCE, "target": TARGET, "case": CASE, "arch": arch, "attempts": [],
           "failed_mnemonics": [], "actions": [], "verified": False}
    try:
        row["verified"] = (actual if phase == "actual" else worker)(arch, row)
        if phase == "actual" and row["verified"]:
            row["guest_file_checked_by_original_test"] = "HELLO FROM UNTRUSTED INPUT\n"
    except Exception as err:
        row["collector_error"] = type(err).__name__
    finally:
        save(phase + "-" + arch, row)
    if not row["verified"]:
        sys.exit(1)
