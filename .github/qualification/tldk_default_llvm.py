"""Build the declared TLDK archive with gVisor's default Linux compiler."""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import native_cross_inputs as evidence


SOURCE = "3f0bd3a70008ffb0d43b4c202deb0133d6d6a823"
TARGET = "@libpluginstack//:libpluginstack"
FLAGS = ["--config=x86_64", "--config=plugin-tldk", "-c", "opt", "--jobs=4",
         "--remote_cache=", "--remote_executor=", "--disk_cache="]


def run(directory, report):
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    bazel = str(directory / "bazel")
    toolchain = "@bazel_tools//tools/cpp:current_cc_toolchain"
    expression = ('[(p.compiler, p.compiler_executable, '
                  '[f.path for f in p.all_files.to_list() if f.basename in ("clang", "clang++")]) '
                  'for p in providers(target).values() if hasattr(p, "compiler")]')
    rc, out, err = evidence.command([bazel, "cquery", *FLAGS, "--output=starlark",
                                     "--starlark:expr=" + expression, toolchain], 300)
    selected = ast.literal_eval(out.decode()) if rc == 0 else []
    clang_path = "external/llvm++llvm_toolchain_minimal+llvm-toolchain-minimal-linux-amd64/bin/clang"
    declared_llvm = any(compiler == "clang" and (exe == clang_path or clang_path in files)
                        for compiler, exe, files in selected)
    report["toolchain_query"] = {"exit": rc, "selected": selected, "declared_llvm": declared_llvm,
                                 "diagnostics": evidence.diagnostics(err)}
    bep = directory / "build.bep"
    rc, _, err = evidence.command([bazel, "build", *FLAGS, "--build_event_publish_all_actions",
                                   "--build_event_json_file=" + str(bep), TARGET], 3000)
    report["build_exit"] = rc
    report["diagnostics"] = evidence.diagnostics(err)
    report["status"] = "DEADLINE" if rc == 124 else "BUILD_FAILED" if rc else "INCOMPLETE"
    # BEP spells external labels canonically. Read the mapped repository name
    # from this invocation rather than hard-coding the module extension name.
    lc, out, msg = evidence.command([bazel, "cquery", *FLAGS, "--output=starlark",
                                     r'--starlark:expr=str(target.label) + "\n" + "\n".join([f.path for f in target.files.to_list()])',
                                     TARGET], 180)
    report["output_query"] = {"exit": lc, "diagnostics": evidence.diagnostics(msg)}
    paths = out.decode().splitlines() if lc == 0 else []
    canonical = paths.pop(0) if paths else None
    if canonical:
        report["canonical_label"] = canonical
    evidence.ROOTS = [canonical, TARGET] if canonical else [TARGET]
    evidence.owned = lambda label: label in evidence.ROOTS
    report["events"] = evidence.events(bep)
    if rc:
        # Keep the actual diagnosis when a dependency failed before the foreign build.
        markers = ("error:", "no such", "not found", "undefined", "not defined", "not provided")
        lines = [line for line in err.splitlines() if any(m.encode() in line.lower() for m in markers)]
        report["errors"] = [line.decode(errors="replace")[:400] for line in lines[:12]
                            if b"://" not in line and b"token" not in line.lower()]
        return
    assert lc == 0, evidence.diagnostics(msg)
    archive, = [Path(line) for line in paths if line.endswith("/libpluginstack.a")]
    root = Path(subprocess.check_output([bazel, "info", "execution_root"], text=True).strip())
    assert not archive.is_absolute() and ".." not in archive.parts
    archive = root / archive
    members = subprocess.check_output(["ar", "t", str(archive)], text=True).splitlines()
    first = next(name for name in members if name.endswith(".o"))
    obj = subprocess.check_output(["ar", "p", str(archive), first])
    assert obj[:6] == b"\x7fELF\x02\x01" and int.from_bytes(obj[18:20], "little") == 62
    report["archive"] = {"size": archive.stat().st_size, "sha256": evidence.digest(archive.read_bytes()),
                         "members": len(members), "sample_member": first, "sample_elf_machine": 62}
    assert declared_llvm
    root_event, = [t for t in report["events"]["targets"] if t["success"]]
    foreign_action, = [a for a in report["events"]["actions"]
                       if a["owner"] == root_event["owner"] and a["success"] and a["exit"] == 0
                       and a["configuration"] == root_event["configuration"]
                       and a["type"].endswith("MakeRule") and a["output"] in paths]
    report["archive_action"] = foreign_action
    assert report["events"]["exit"]["name"] == "SUCCESS"
    report["status"] = "PASS"


if __name__ == "__main__":
    directory = Path(os.environ["RUNNER_TEMP"]) / "tldk-llvm"
    result = {"source": SOURCE, "target": TARGET, "flags": FLAGS, "status": "INCOMPLETE"}
    try:
        run(directory, result)
    except (ValueError, OSError, AssertionError, StopIteration, subprocess.CalledProcessError) as error:
        result["reader_error"] = type(error).__name__ + ": " + str(error)[:400]
    (directory / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    print(result["status"])
    sys.exit(0 if result["status"] == "PASS" else 1)
