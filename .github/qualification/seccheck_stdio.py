"""Compile on the published GNU and musl platforms without executing the server."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


SOURCE = "a81045f739c45596265f889dff2a4cbc283a9144"
SERVER = "//examples/seccheck:server_cc"
POINTS = "//pkg/sentry/seccheck/points:points_cc_proto"
TMP = Path(os.environ["RUNNER_TEMP"])
WORK = TMP / "seccheck-stdio"
RECEIPTS = WORK / "receipts"
BAZEL = [TMP / "seccheck-bazel", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3000m"]
FLAGS = ["--lockfile_mode=off", "--jobs=3", "--remote_executor=", "--remote_cache=", "--bes_backend=", "--color=no", "--curses=no"]
PLATFORMS = [
    ("gnu-amd64", "//tools/bazeldefs:linux_amd64", "x86_64-linux-gnu", 62),
    ("musl-amd64", "@llvm//platforms:linux_x86_64_musl", "x86_64-linux-musl", 62),
    ("musl-arm64", "@llvm//platforms:linux_aarch64_musl", "aarch64-linux-musl", 183),
]


def run(name, command):
    log = WORK / (name + ".log")
    with log.open("w") as output:
        proc = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
    return proc.returncode, log.read_text(errors="replace")


def file_digest(path):
    with path.open("rb") as source:
        digest = hashlib.sha256()
        while chunk := source.read(1048576):
            digest.update(chunk)
        return digest.hexdigest()


def qualify(name, platform, triple, machine):
    options = [*FLAGS, "--platforms=" + platform]
    bep = WORK / (name + ".bep")
    print(f"Compiling {name} ({platform})", flush=True)
    code, log = run(name, [*BAZEL, "build", *options, "--keep_going",
                          "--build_event_json_file=" + str(bep), POINTS, SERVER])
    lines = log.splitlines()
    indices = {j for i, line in enumerate(lines) if re.search(r"error:|fatal:|^ERROR:|failed to build|^Target ", line, re.I)
               for j in range(max(0, i - 1), min(len(lines), i + 5))}
    (RECEIPTS / (name + "-diagnostics.txt")).write_text("\n".join(lines[i] for i in sorted(indices)[:220]) + "\n")
    query = WORK / (name + ".actions")
    with query.open("w") as output:
        process = subprocess.run([*BAZEL, "aquery", *options, "--include_param_files", "--output=jsonproto",
                                  f'mnemonic("CppCompile", {SERVER})'], stdout=output, stderr=subprocess.PIPE, text=True)
    query_code = process.returncode
    if query_code:
        (RECEIPTS / (name + "-query.txt")).write_text(process.stderr[-6000:])
    actions = json.loads(query.read_text()).get("actions", []) if not query_code and query.stat().st_size else []
    compiler_arguments = []
    for action in actions:
        args = action.get("arguments", [])
        for parameter_file in action.get("paramFiles", []):
            args = args + parameter_file.get("arguments", [])
        for position, arg in enumerate(args):
            if arg in ("-target", "--target", "--sysroot") and position + 1 < len(args):
                compiler_arguments.append(arg + "=" + args[position + 1])
            elif arg.startswith(("--target=", "--sysroot=", "-target=")):
                compiler_arguments.append(arg)
    statuses = {}
    if bep.exists():
        with bep.open() as stream:
            for line in stream:
                event = json.loads(line)
                target = event.get("id", {}).get("targetCompleted", {})
                label = target.get("label", "").lstrip("@")
                if label in (SERVER, POINTS) and not target.get("aspect"):
                    statuses[label] = event.get("completed", {}).get("success", False)
    diagnostics = {}
    for field in ("stdin", "stdout", "stderr"):
        errors = []
        for i, line in enumerate(lines):
            if "sentry.pb.h" in line and re.search(r"\berror:", line):
                context = lines[i:i+7]
                if any(re.search(r"\b" + field + r"\b", s) for s in context):
                    errors.append(context[:4])
        diagnostics[field] = errors[:2]
    result = {"platform": platform, "source": SOURCE, "build_exit": code, "target_success": statuses,
              "compiler_args": sorted(set(compiler_arguments)), "configuration_query_exit": query_code,
              "generated_header_errors": diagnostics}
    if code == 0:
        binary = Path("bazel-bin/examples/seccheck/server_cc")
        if binary.exists():
            with binary.open("rb") as stream:
                header = stream.read(20)
            result["elf"] = {"sha256": file_digest(binary), "machine": int.from_bytes(header[18:20], "little"),
                             "elf64": header[:6] == bytes.fromhex("7f454c460201")}
    correct_toolchain = any(arg in ("--target=" + triple, "-target=" + triple) for arg in compiler_arguments)
    compiled = statuses == {SERVER: True, POINTS: True} and code == 0 and result.get("elf", {}).get("elf64") and result["elf"]["machine"] == machine
    reproduced = name.startswith("musl") and code != 0 and all(diagnostics.values()) and statuses == {SERVER: False, POINTS: False}
    result["classified"] = bool(correct_toolchain and (compiled or reproduced))
    print(name, json.dumps({k: v for k, v in result.items() if k != "generated_header_errors"}), flush=True)
    (RECEIPTS / (name + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    return result["classified"]


RECEIPTS.mkdir(parents=True)
outcomes = [qualify(*platform) for platform in PLATFORMS]
raise SystemExit(0 if all(outcomes) else 1)
