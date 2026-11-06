"""Exercise the public syscall Make target; publish only typed evidence."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from urllib.parse import unquote, urlparse

STOCK = "9be3ad12e8a5acc4aeb3b2cbf59108de2387a45e"
FIXED = "7372ed71d71ca50fa545722ba6051b059fc96835"
TOOLCHAIN_MODULE = "574594e294edde57f90c93958ee7d089c73758d7"
OWNER = "//test/syscalls:eventfd_test_runsc_systrap_shared"
WORKER = "//test/rbe/release_prewarmer:amd64"
IMAGE = "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
STAGE = Path(os.environ["RUNNER_TEMP"]) / "make-syscalls"
RUNTIME = "make-syscalls-" + os.environ["GITHUB_RUN_ID"]
INSTALL = Path("/tmp") / RUNTIME
RECEIPT = {"stock": STOCK, "fixed": FIXED, "product": "7e2c0ff44c64f2c4261c876abb687cbbf5f80ed2",
           "owner": OWNER, "phases": {}}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write():
    (STAGE / "result.json").write_text(json.dumps(RECEIPT, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def switch(sha):
    assert sha in (STOCK, FIXED)
    assert not git("diff", "--name-only", "HEAD")
    subprocess.run(["git", "switch", "--quiet", "--detach", sha], check=True)
    assert git("rev-parse", "HEAD") == sha
    assert git("rev-parse", "HEAD:MODULE.bazel") == TOOLCHAIN_MODULE
    assert git("rev-parse", "HEAD:test/runner/defs.bzl") == "a24d7ea7b8c8e530c1ef05b8f4f4557c6e0adbe4"


def collect_bep(path, row):
    row["tests"] = []
    row["failed_actions"] = []
    if not path.exists():
        return
    for line in path.open():
        event = json.loads(line)
        if "finished" in event:
            row["bazel_exit"] = event["finished"]["exitCode"].get("code", 0)
        key = event.get("id", {})
        if key.get("testResult", {}).get("label") == OWNER:
            result = event["testResult"]
            entry = {"status": result["status"], "shard": key["testResult"].get("shard", 0),
                     "cached": result.get("cachedLocally", False) or result.get("executionInfo", {}).get("cachedRemotely", False)}
            for artifact in result.get("testActionOutput", []):
                if artifact.get("name") == "test.log":
                    uri = artifact.get("uri", "")
                    match = re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/[0-9]+$", uri)
                    if match:
                        entry["log_sha256"] = match[1]
                    elif uri.startswith("file://"):
                        local = Path(unquote(urlparse(uri).path))
                        if local.is_file():
                            entry["log_sha256"] = digest(local.read_bytes())
            row["tests"].append(entry)
        ac = event.get("action", {})
        if ac and not ac.get("success", False) and len(row["failed_actions"]) < 8:
            owner = key.get("actionCompleted", {}).get("label", "")
            mnemonic = ac.get("type", "")
            if re.fullmatch(r"[@/\w:+.~@-]*", owner) and re.fullmatch(r"[\w-]*", mnemonic):
                row["failed_actions"].append([owner, mnemonic, ac.get("exitCode")])


def collect_execution(compact, row):
    row["test_spawns"] = []
    if not compact.is_file():
        return
    # Project a fixed allowlist before leaving the runner. Other env values and
    # process arguments are private and need not be retained.
    expr = """select(.spawn) | .spawn | select(.mnemonic=="TestRunner" and .targetLabel==$owner) |
                [(.runner//""),(.cacheHit//false),(.exitCode//0),
                 ([.platform.properties[]?|{key:.name,value:.value}]|from_entries),
                 ([.envVars[]?|select(.name=="RUNTIME" or .name=="GVISOR_SIDECAR_BINARIES_DIR" or .name=="PARTITION" or .name=="TOTAL_PARTITIONS" or .name=="HOST_KERNEL")|{key:.name,value:.value}]|from_entries)]"""
    with subprocess.Popen([STAGE/"bb", "print", "--raw", "--compact_execution_log="+str(compact)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        p = subprocess.run(["jq", "-c", "--arg", "owner", OWNER, expr], stdin=decoder.stdout,
                           capture_output=True, check=True, text=True)
        assert decoder.wait() == 0
    for line in p.stdout.splitlines():
        runner, cached, code, props, env = json.loads(line)
        assert runner in ("remote", "linux-sandbox", "local", "processwrapper-sandbox")
        row["test_spawns"].append({"runner": runner, "cached": cached, "exit": code,
                                  "firecracker": props.get("workload-isolation-type") == "firecracker",
                                  "image": props.get("container-image") == IMAGE,
                                  "root": props.get("dockerUser") == "root",
                                  "ipv6": props.get("network-enable-ipv6") == "true",
                                  "install_runtime": env.get("RUNTIME") == str(INSTALL/"runsc"),
                                  "runtime_env_present": "RUNTIME" in env,
                                  "install_sidecars": env.get("GVISOR_SIDECAR_BINARIES_DIR") == str(INSTALL/"gvisor-bin"),
                                  "sidecars_env_present": "GVISOR_SIDECAR_BINARIES_DIR" in env,
                                  "partition_passed": env.get("PARTITION") == str(row["partition"]),
                                  "total_passed": env.get("TOTAL_PARTITIONS") == "2",
                                  "host_kernel_passed": env.get("HOST_KERNEL") == os.uname().release})


def log_evidence(row, make_log):
    output = Path("bazel-testlogs/test/syscalls/eventfd_test_runsc_systrap_shared")
    names = []
    observations = []
    recorded = {x["log_sha256"] for x in row["tests"] if "log_sha256" in x}
    for p in sorted(output.glob("**/test.log")):
        data = p.read_bytes()
        sha = digest(data)
        if recorded and sha not in recorded:
            continue
        if not row["tests"]:
            continue
        text = data.decode(errors="replace")
        names.extend(re.findall(r"\[\s*OK\s*\]\s+(EventfdTest\.\w+)", text))
        error_lines = [l for l in text.splitlines() if ("no such file" in l.lower() or "not found" in l.lower())]
        observations.append({"sha256": sha, "bytes": len(data), "source_bound": sha in recorded,
                             "client_sidecar_mentioned": str(INSTALL/"gvisor-bin") in text,
                             "client_sidecar_missing": any(str(INSTALL/"gvisor-bin") in l for l in error_lines),
                             "sentry_missing": any("sentry" in l.lower() for l in error_lines),
                             "runfiles_launcher_mentioned": "release/runsc" in text or "/release/runsc" in text,
                             "operation_not_permitted": "operation not permitted" in text.lower()})
    row["guest_cases"] = sorted(names)
    row["logs"] = observations
    make_text = make_log.decode(errors="replace")
    row["make_log_sha256"] = digest(make_log)
    row["install"] = {"stage_action": "--- COPY //:release" in make_text,
                      "runsc": (INSTALL/"runsc").is_file(), "sentry": (INSTALL/"gvisor-bin/gvisor_sentry").is_file()}
    row["diagnostic_classes"] = [key for key, marker in {
        "no_matching_execution_platform": "No matching execution platform",
        "no_such_file": "No such file or directory", "no_target": "no such target",
        "compiler_error": "error:", "worker_unavailable": "UNAVAILABLE", "disk_full": "No space left on device",
        "unauthorized": "UNAUTHENTICATED", "sandbox_permission": "Operation not permitted",
        "invalid_bazel_option": "while parsing option", "unknown_bazel_option": "unrecognized option",
        "timed_out": "internal error: timed out", "no_tests": "No test targets were found",
        "no_make_rule": "No rule to make target",
    }.items() if marker.lower() in make_text.lower()]


def run_phase(name, source, part, explicit=False):
    switch(source)
    d = STAGE / name
    d.mkdir()
    row = {"source": source, "partition": part, "explicit_override": explicit, "invocation": str(uuid.uuid4())}
    RECEIPT["phases"][name] = row
    write()
    options = ["--config=syscalls-amd64", "--nocache_test_results", "--flaky_test_attempts=1",
               "--test_timeout=300", "--remote_download_outputs=toplevel",
               "--invocation_id="+row["invocation"], "--execution_log_compact_file="+str(d/"exec.zst"),
               "--build_event_json_file="+str(d/"bep"), "--build_event_publish_all_actions",
               "--modify_execution_info=TestRunner=+no-remote-cache"]
    flags = ["--config=x86_64", "-c", "opt", "--jobs=40", "--lockfile_mode=off", "--remote_download_outputs=toplevel",
             "--extra_execution_platforms="+WORKER, "--host_platform="+WORKER,
             "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
             "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
             "--@rules_python//python/config_settings:bootstrap_impl=native"]
    make = ["make", "DOCKER_BUILD=false", "BAZEL_TEST_OUTPUT=errors",
            "STARTUP_OPTIONS=--nosystem_rc --nohome_rc --bazelrc="+str(STAGE/"private.bazelrc"),
            "BAZEL_OPTIONS="+" ".join(flags), "OPTIONS="+" ".join(options), "TARGETS="+OWNER,
            "PARTITION="+str(part), "TOTAL_PARTITIONS=2", "RUNTIME="+RUNTIME]
    if explicit:
        assert (INSTALL/"runsc").is_file() and (INSTALL/"gvisor-bin/gvisor_sentry").is_file()
        make += ["RUNTIME_BIN="+str(INSTALL/"runsc"), "RUNTIME_DIR="+str(INSTALL)]
    timeout = 1300 if name in ("stock", "fixed1") else 480
    with (d/"make.log").open("wb") as log:
        proc = subprocess.run(["timeout", "--kill-after=20", str(timeout), *make, "syscall-tests"],
                              stdout=log, stderr=subprocess.STDOUT)
    row["make_exit"] = proc.returncode
    collect_bep(d/"bep", row)
    write()
    collect_execution(d/"exec.zst", row)
    log_evidence(row, (d/"make.log").read_bytes())
    write()
    return row


def validate():
    phases = RECEIPT["phases"]
    assert set(phases) == {"stock", "fixed1", "fixed2", "override"}
    for key, row in phases.items():
        staged = key in ("stock", "override")
        assert row["test_spawns"] and row["tests"] and not any(t["cached"] for t in row["tests"]), key
        for spawn in row["test_spawns"]:
            assert spawn["runner"] == "remote" and not spawn["cached"] and spawn["root"] and spawn["firecracker"] and spawn["image"] and spawn["ipv6"], key
            assert spawn["partition_passed"] and spawn["host_kernel_passed"] and spawn["total_passed"], key
            assert spawn["runtime_env_present"] == spawn["install_runtime"] == staged, key
            assert spawn["sidecars_env_present"] == spawn["install_sidecars"] == staged, key
        assert row["logs"] and all(log["source_bound"] for log in row["logs"]), key
        if staged:
            if row["make_exit"] != 0:
                assert any(log["client_sidecar_missing"] and log["sentry_missing"] for log in row["logs"]), key
        else:
            assert row["make_exit"] == row["bazel_exit"] == 0, key
            assert all(t["status"] == "PASSED" for t in row["tests"]), key
    a, b = [phases[k]["guest_cases"] for k in ("fixed1", "fixed2")]
    src = Path("test/syscalls/linux/eventfd.cc").read_text()
    expected = sorted("EventfdTest."+n for n in re.findall(r"^TEST\(EventfdTest, (\w+)\)", src, re.M))
    assert len(expected) == 11 and a and b and not set(a).intersection(b) and sorted(a+b) == expected


try:
    run_phase("stock", STOCK, 1)
    first = run_phase("fixed1", FIXED, 1)
    if first["make_exit"] == 0:
        run_phase("fixed2", FIXED, 2)
    if first["test_spawns"] and (INSTALL/"runsc").is_file() and (INSTALL/"gvisor-bin/gvisor_sentry").is_file():
        run_phase("override", FIXED, 1, explicit=True)
    validate()
    RECEIPT["verified"] = True
finally:
    write()
