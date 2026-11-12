"""Compare the actual LLVM Support compile and its generated toolchain map."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path.cwd()
E2E = ROOT / "e2e/rules_cc"
TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "sidecar-evidence"
OUT.mkdir()
SOURCE = Path(__file__).resolve().parent
PACKAGE = E2E / "support_map_probe"
PACKAGE.mkdir()
shutil.copyfile(SOURCE / "support.BUILD", PACKAGE / "BUILD.bazel")
shutil.copyfile(SOURCE / "select_object.bzl", PACKAGE / "select_object.bzl")
CLI = ["bazel", f"--bazelrc={TMP / 'sidecar.bazelrc'}"]
FLAGS = ["--config=remote", "--jobs=60", "--nocheck_visibility", "--experimental_output_paths=strip"]
PROBE = "//support_map_probe:object"
QUERY = f'inputs(".*PrettyStackTrace.cpp", mnemonic("CppCompile", deps({PROBE})))'
BASE = "8a3f5bd77702fc0de58252e715404fcac1eed24e"
STAGES = "baa2066bd9f6830d078228a9047649f8334f712e"
COMBINED = "b253312fd2f6b047d8a70781c7623f6e2c1d4064"
RESULTS = {}


def record():
    (OUT / "report.json").write_text(json.dumps(RESULTS, indent=2) + "\n")


def bazel(name, command, args):
    result = subprocess.run(
        [*CLI, command, *FLAGS, *args], cwd=E2E, capture_output=True, text=True,
    )
    lines = []
    for line in result.stderr.splitlines():
        if any(word in line.lower() for word in ("header=", "api-key", "credential")):
            continue
        if any(word in line for word in (
            "ERROR:", "Invocation ID:", "does not depend on a module",
            "fatal error:", "Build completed", "did NOT complete", "processes:",
        )):
            lines.append(line)
    (OUT / f"{name}.log").write_text("\n".join(lines) + "\n")
    print(name, result.returncode, "\n".join(lines[-4:]), flush=True)
    return result


def inspect(name, revision, *, caller_args=False):
    subprocess.run(["git", "switch", "--quiet", "--detach", revision], cwd=ROOT, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert head == revision
    flags = []
    config_file = ROOT / "toolchain/BUILD.bazel"
    original = config_file.read_text()
    if caller_args:
        before = 'stage_independent_args_list(\n    name = "complete_toolchain_args",'
        assert original.count(before) == 1
        config_file.write_text(original.replace(before, before.replace("stage_independent_args_list", "cc_args_list")))
    try:
        row = RESULTS[name] = {"head": head, "callerArgs": caller_args, "cppPathMapping": False}
        record()
        query = bazel(name + "-action", "aquery", [
            *flags, "--include_param_files", "--noinclude_artifacts", "--output=jsonproto", QUERY,
        ])
        query.check_returncode()
        graph = json.loads(query.stdout)
        actions = [
            action for action in graph["actions"]
            if "-D_LIBCPP_ABI_NAMESPACE=__InternalSymbolizer" in action["arguments"]
            and "internal_symbolizer_linux_x86_64_gnu_2_28-dbg-" in action["arguments"][action["arguments"].index("-o") + 1]
        ]
        assert len(actions) == 1, len(actions)
        action = actions[0]
        args = action["arguments"]
        # This qualification requests one existing action via the production
        # symbolizer. Compare every argument with the real top-level test
        # graph before compiling; an execution-platform Support is different.
        production = bazel(name + "-fission-graph", "aquery", [
            "--include_param_files", "--noinclude_artifacts", "--output=jsonproto",
            QUERY.replace(PROBE, "//:asan_fission_debug_package_test"),
        ])
        production.check_returncode()
        identical = sum(a["arguments"] == args for a in json.loads(production.stdout)["actions"])
        row.update(argumentCount=len(args), identicalFissionActions=identical)
        record()
        assert identical == 1, row
        if revision == STAGES:
            assert len(args) == 141, len(args)
        compiler = next(v for v in args if v.endswith("/clang++"))
        inc = [args[i + 1] for i, v in enumerate(args) if v == "-isystem"]
        maps = [v.split("=", 1)[1] for v in args if v.startswith("-fmodule-map-file=") and v.endswith("/module_map.modulemap")]
        assert len(maps) == 1 and inc
        obj = args[args.index("-o") + 1]
        row.update(compiler=compiler, includes=inc, moduleMap=maps[0], object=obj,
                   configuration=graph.get("configuration", []), executionInfo=action.get("executionInfo", []))
        (OUT / (name + ".argv.json")).write_text(json.dumps(args, indent=2) + "\n")
        record()
        # Download the independently requested module map even if the compiler
        # subsequently fails. Its exec-root path must equal the compiler's input.
        map_run = bazel(name + "-map", "build", [*flags, "--remote_download_outputs=toplevel", "--output_groups=physical_map", PROBE])
        map_run.check_returncode()
        map_file = E2E / maps[0]
        data = map_file.read_bytes()
        row["mapSHA256"] = hashlib.sha256(data).hexdigest()
        row["headers"] = {}
        for header in ("type_traits", "utility", "iterator", "cstddef", "stddef.h"):
            mapped_paths = [h.decode() for h in re.findall(rb'textual header "([^"]+/' + header.encode() + rb')"', data)]
            row["headers"][header] = {"map": mapped_paths, "searched": [str(Path(i) / header) for i in inc]}
        traits = row["headers"]["type_traits"]
        row["typeTraitsPathMatches"] = any(
            mapped.endswith(search) for mapped in traits["map"] for search in traits["searched"]
        )
        record()
        result = bazel(name + "-object", "build", [*flags, "--remote_download_outputs=toplevel", PROBE])
        row["objectExit"] = result.returncode
        row["missingModuleExport"] = "does not depend on a module exporting" in result.stderr
        if result.returncode == 0:
            contents = (E2E / obj).read_bytes()
            row["objectMagic"] = contents[:4].hex()
            row["bitcode"] = contents[:4] in (b"BC\xc0\xde", b"\xde\xc0\x17\x0b")
            row["objectSHA256"] = hashlib.sha256(contents).hexdigest()
            assert row["bitcode"]
        print(name, "type_traits", json.dumps(row["headers"]["type_traits"]), flush=True)
        record()
        return row
    finally:
        if caller_args:
            config_file.write_text(original)


os.environ["USE_BAZEL_VERSION"] = (ROOT / ".bazelversion").read_text().strip()
baseline = inspect("base", BASE)
stages = inspect("original", STAGES)
combined = inspect("combined", COMBINED)
fixed = inspect("caller-configuration", COMBINED, caller_args=True)
for row in (baseline, fixed):
    assert row["typeTraitsPathMatches"] and row["objectExit"] == 0, row
for row in (stages, combined):
    assert not row["typeTraitsPathMatches"] and row["objectExit"] and row["missingModuleExport"], row
record()
