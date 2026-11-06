"""Check which configured compiler and archive the four platform profiles select."""

import json
from pathlib import Path
import subprocess
import sys

ROOT = "@llvm//platforms:"
STATIC = "linux_x86_64_glibc_static"
SELECTED = {"toolchain/args:unwindlib_compiler_rt", "runtimes/libunwind:static_unwind"}
STUB = {"toolchain/args:unwindlib_none", "runtimes/libunwind:unwind"}
FILTER = '"(toolchain/args:unwindlib_(none|compiler_rt)|runtimes/libunwind:(static_unwind|unwind))$"'
DIRECT = (
    'deps(@llvm//toolchain:unwindlib, 1) + '
    'deps(@llvm//runtimes/libunwind:libunwind_library_search_directory, 1)'
)
RUNTIME = 'deps(@llvm//runtimes/libunwind:libunwind.static)'
EXPR = (
    'str(target.label) + "\\t" + ",".join([str(v) for k, v in '
    'build_options(target).items() if str(k).endswith("//toolchain:runtime_stage")])'
)


def configured(platform: str, scope: str) -> list[tuple[str, str]]:
    command = [
        "bazel", "cquery", "--config=x86_64", "-c", "opt", "--jobs=4",
        "--remote_cache=", "--remote_executor=", "--disk_cache=",
        "--platforms=" + ROOT + platform, "--output=starlark",
        "--starlark:expr=" + EXPR, f"filter({FILTER}, {scope})",
    ]
    output = subprocess.check_output(command, text=True, timeout=300)
    rows = []
    for line in output.splitlines():
        label, stage = line.split("\t")
        rows.append((label.split("//", 1)[1], stage))
    return sorted(set(rows))


def main(path: Path) -> None:
    records: dict[str, list[tuple[str, str]]] = {}
    for platform in (STATIC, "linux_x86_64_gnu.2.44", "linux_x86_64_musl", "none_wasm32"):
        records[platform] = configured(platform, DIRECT)
        path.write_text(json.dumps(records, indent=2) + "\n")
        selected = {label for label, _ in records[platform]}
        expected = SELECTED if platform == STATIC else STUB
        if selected != expected:
            raise ValueError(f"{platform} selected {selected}, expected {expected}")

    records["libunwind bootstrap"] = configured(STATIC, RUNTIME)
    path.write_text(json.dumps(records, indent=2) + "\n")
    stage0 = {label for label, stage in records["libunwind bootstrap"] if stage == "stage0"}
    if stage0 != STUB:
        raise ValueError(f"libunwind stage0 selected {stage0}, expected {STUB}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
