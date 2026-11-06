"""Compare only the two ARM64 public runsc binaries, built with stock output mapping."""

import collections
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from elf import file_table, sections, sha


SOURCE = "bb983b988802b854bd03dd7a0a5220e7e7b4440e"
ORIGINAL_TABLE = "63a6b533535a0c1375e5a7317cc74f9e281ebbfbf6970c1585b5ec9ff054f4ab"
CPU = re.compile(rb"(?:^|/)bazel-out/(cfg|(?:aarch64|arm64|k8|x86_64)-(?:opt|fastbuild|dbg)(?:-exec)?(?:-ST-[a-f0-9]{6,64})?)/bin/")
FLAGS = [
    "-c", "opt", "--experimental_output_paths=strip", "--jobs=4", "--lockfile_mode=off",
    "--color=no", "--curses=no", "--noshow_progress",
    "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
    "--@rules_python//python/config_settings:bootstrap_impl=native",
]
DIRECT = [
    "--config=aarch64", "--platforms=@llvm//platforms:linux_aarch64_glibc_static",
    "--host_platform=//test/rbe/release_prewarmer:arm64",
    "--extra_execution_platforms=//test/rbe/release_prewarmer:arm64",
    "--extra_toolchains=//test/rbe/go_bootstrap:linux_arm64_ash",
]
OUT = Path(os.environ["RUNNER_TEMP"]) / "mapped-runsc"
RECEIPT = OUT / "result.json"


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def build(mode, row):
    flags = [*FLAGS, *(DIRECT if mode == "direct" else ["--config=x86_64"])]
    target = "//runsc" if mode == "direct" else "//test/rbe/release_prewarmer:cli_only"
    bazel = str(OUT / "bazel")
    subprocess.run([bazel, "build", *flags, target], check=True)
    row["built"] = True
    paths = output(bazel, "cquery", *flags, "--output=files",
                   f'filter("//runsc:runsc$", deps({target}))').splitlines()
    [binary] = map(Path, paths)
    assert binary.parts[0] == "bazel-out" and "bin" in binary.parts
    row["configuration"] = binary.parts[binary.parts.index("bin") - 1]
    assert binary.parts[binary.parts.index("bin") + 1] == "runsc" and binary.name == "runsc"
    go_version = subprocess.run(["go", "version", "-m", str(binary)], capture_output=True, text=True, env={**os.environ, "GOTOOLCHAIN": "local"})
    version = re.search(r"\bgo1[.]\d+[.]\d+$", go_version.stdout.partition("\n")[0])
    row["go_version"] = version[0] if version else None
    raw = binary.read_bytes()
    parts = sections(raw)
    row.update(size=len(raw), sha256=sha(raw), elf_machine=183)
    output_lines = output(str(binary), "--version").splitlines()
    row["original_stamp"] = "runsc version bb983b988802" in output_lines
    filenames = file_table(parts)
    assert filenames
    normalized = [CPU.sub(b"/bazel-out/CONFIG/bin/", name) for name in filenames]
    row["filenames"] = {
        "total": len(filenames),
        "configurations": dict(collections.Counter(m[1].decode() for name in filenames for m in CPU.finditer(name))),
        "ordered_sha256": sha(b"\0".join(filenames)),
        "normalized_ordered_sha256": sha(b"\0".join(normalized)),
    }
    row["sections"] = {
        name: {"size": parts[name][1], "sha256": sha(parts[name][0])}
        for name in (".text", ".rodata", ".gopclntab", ".data.rel.ro.gopclntab", ".data",
                     ".debug_info", ".debug_line", ".go.buildinfo", ".note.go.buildid", ".note.gnu.build-id")
        if name in parts
    }
    assert row["original_stamp"]


def main():
    assert output("git", "rev-parse", "HEAD") == SOURCE and os.uname().machine == "aarch64"
    result = {"source": SOURCE, "checked_in_llvm": "0.8.24", "rules_go": "0.63.0", "cases": {}}
    for mode in ["direct", "paired"]:
        row = result["cases"][mode] = {"built": False}
        try:
            build(mode, row)
            row["complete"] = True
        except (AssertionError, ValueError, OSError, subprocess.CalledProcessError) as error:
            row.update(complete=False, error=type(error).__name__)
            print(f"{mode}: {error}", file=sys.stderr)
        RECEIPT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    direct, paired = result["cases"].values()
    assert direct["complete"] and paired["complete"] and direct["configuration"] != paired["configuration"]
    result["equal_full_ELF"] = direct["sha256"] == paired["sha256"]
    result["equal_ordered_filenames"] = direct["filenames"]["ordered_sha256"] == paired["filenames"]["ordered_sha256"]
    result["equal_original_after_normalization"] = all(
        r["filenames"]["normalized_ordered_sha256"] == ORIGINAL_TABLE for r in (direct, paired)
    )
    result["different_sections"] = sorted(
        s for s in direct["sections"].keys() | paired["sections"].keys() if direct["sections"].get(s) != paired["sections"].get(s)
    )
    RECEIPT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
