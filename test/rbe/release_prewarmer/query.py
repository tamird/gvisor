"""Bind the configured action platforms to the archived native test results."""
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

root = Path(os.environ["RUNNER_TEMP"]) / "seccheck-query"
product = "c6b932f3d47aee23d984d75cd3b696991f772eb5"
server = "//examples/seccheck:server_cc"
test = "//pkg/sentry/seccheck/sinks/remote:remote_test"


def original(archive, digest, arch):
    raw = (root / archive).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest
    with ZipFile(io.BytesIO(raw)) as z:
        return json.loads(z.read(arch + ".json"))


def configured(label, mnemonic, flags):
    query = f'mnemonic("{mnemonic}", {label})'
    cmd = [str(root / "bazel"), "--nosystem_rc", "--nohome_rc", "aquery",
           *flags, "--output=jsonproto", "--include_param_files", query]
    graph = json.loads(subprocess.check_output(cmd, timeout=480))
    targets = {t["id"]: t["label"].lstrip("@") for t in graph["targets"]}
    configs = {c["id"]: c for c in graph["configuration"]}
    actions = []
    for a in graph.get("actions", []):
        if targets[a["targetId"]] != label or a["mnemonic"] not in mnemonic.split("|"):
            continue
        args = a.get("arguments", []) + [
            arg for param in a.get("paramFiles", []) for arg in param.get("arguments", [])
        ]
        cfg = configs[a["configurationId"]]
        row = {"mnemonic": a["mnemonic"], "platform": a.get("executionPlatform","").lstrip("@"),
               "configuration": cfg["checksum"], "target_cpu": cfg.get("platformName")}
        if label == server:
            assert args, a["mnemonic"]
            row["argv_sha256"] = hashlib.sha256(json.dumps(args).encode()).hexdigest()
        actions.append(row)
    return actions


def check(arch, cpu, target_cpu):
    compiler = original("compile.zip", "54ca15bf420c9db5aa1e552934c3005c2175f7ec84fc2f59f0a6c2e5b9a8fcb2", arch)
    runtime = original("run.zip", "edf23fd4637ca3d6abfc1bd8f8a6c63c3b1e0a4696ae04eeab0b9e4e66145ea9", arch)
    assert compiler["source"] == runtime["source"] == product
    assert compiler["compiler"]["verified"] and compiler["helper_qualified"]
    assert compiler["seccheck_server"]["elf"] == runtime["seccheck_server"]["elf"]
    result, = runtime["targets"][test]["results"]
    spawn, = runtime["targets"][test]["actions"]
    assert result["xml"]["named"] == {"passed": ["TestExample"], "failed": [], "skipped": []}
    assert runtime["native_exit"] == spawn["exit"] == 0 and result["status"] == "PASSED"
    assert spawn["runner"] == "remote" and spawn["matched_platform"] and spawn["native_wrapper"]
    assert not (spawn["cache_hit"] or result["cached_local"] or result["cached_remote"])
    assert runtime["actions"]["expected_platform"] == compiler["actions"]["expected_platform"]
    tool_platform, test_platform = runtime["execution_platforms"]
    flags = ["--config="+cpu, "--extra_execution_platforms="+tool_platform+","+test_platform,
             "--host_platform="+tool_platform, "--platforms="+runtime["target_platform"],
             "--extra_toolchains=//test/rbe/go_bootstrap:linux_"+arch+"_ash",
             "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
             "--noincompatible_exclude_starlark_flags_from_exec_config",
             "--incompatible_use_native_posix_test_wrapper", "--test_filter=^TestExample$",
             "--nocache_test_results", "--test_env=GO_TEST_WRAP_TESTV=1",
             "--runs_per_test=1", "--flaky_test_attempts=1", "--build_tag_filters=-network_plugins",
             "--modify_execution_info=^TestRunner$=+no-remote-cache",
             "--lockfile_mode=off", "--jobs=32", "--color=no", "--curses=no"]
    record = {"source": product, "arch": arch, "actual_test_configuration": result["config"],
              "actual_worker": runtime["actions"]["expected_platform"],
              "actual_test_action": spawn["digest"], "actual_test": "TestExample",
              "actual_test_invocation": runtime["requested_invocation"],
              "actual_helper": runtime["seccheck_server"]["elf"], "target_cpu": target_cpu,
              "compiler_artifact": 13463757999, "runtime_artifact": 13473985103}
    try:
        c = record["compiler"] = configured(server, "CppCompile|CppLink", flags)
        t = record["test"] = configured(test, "TestRunner", flags)
        record["checks"] = {
            "compile_and_link": {a["mnemonic"] for a in c} == {"CppCompile", "CppLink"},
            "test_runner": len(t) == 1,
            "compiler_platform": bool(c) and all(a["platform"] == tool_platform for a in c),
            "test_platform": bool(t) and all(a["platform"] == test_platform for a in t),
            "test_configuration": bool(t) and all(a["configuration"] == result["config"] for a in t),
            "original_compile_arguments": sorted(a["argv_sha256"] for a in c) == sorted(a["argv_sha256"] for a in compiler["compiler"]["actions"]),
            "original_compile_cpu": all(a["cpu"] == target_cpu for a in compiler["compiler"]["actions"]),
        }
        return all(record["checks"].values())
    finally:
        (root / "public" / (arch+".json")).write_text(json.dumps(record, indent=2)+"\n")


if __name__ == "__main__":
    arch, cpu, target_cpu = sys.argv[1:]
    raise SystemExit(0 if check(arch,cpu,target_cpu) else 1)
