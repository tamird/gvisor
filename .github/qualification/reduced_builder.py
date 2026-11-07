"""Check the reduced Ubuntu builder around the existing ARM64 ELF verifier."""

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

import runtime_cross as runtime


SOURCE = "c115840f8a85010111fa5d72b611d7d9ab1df34b"
FOREIGN_PACKAGES = (
    "crossbuild-essential-amd64",
    "crossbuild-essential-arm64",
    "gcc-aarch64-linux-gnu",
    "g++-aarch64-linux-gnu",
    "binutils-aarch64-linux-gnu",
)
FOREIGN_TOOLS = ("gcc", "g++", "as", "ld", "nm", "objcopy")
BAZEL = "/usr/local/bin/bazel"


def installed_version(name):
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", name],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 1 and not result.stdout:
        return None
    result.check_returncode()
    return result.stdout.strip()


def output(*argv):
    return subprocess.check_output(argv, text=True).strip()


def main():
    runtime.BAZEL = runtime.q.BAZEL = BAZEL
    result = {
        "source": SOURCE,
        "host": platform.machine(),
        "image_id": os.environ["BUILDER_IMAGE"],
        "scope": "ARM64 runsc and Sentry built in the reduced AMD64 Ubuntu image; QEMU user-mode CLI version",
        "roots": runtime.ROOTS,
        "arm64": {},
    }
    path = runtime.TASK / "verdict.json"

    def save():
        data = json.dumps(result, indent=2)
        assert len(data) < 524288
        path.write_text(data + "\n")

    save()
    assert output("git", "rev-parse", "HEAD") == SOURCE
    assert result["host"] == "x86_64"
    result["bazel"] = output(BAZEL, "--version")
    result["build_essential"] = installed_version("build-essential")
    result["native_gcc"] = output("gcc", "-dumpmachine")
    result["foreign_packages"] = {p: installed_version(p) for p in FOREIGN_PACKAGES}
    result["foreign_tools"] = {
        t: shutil.which("aarch64-linux-gnu-" + t) for t in FOREIGN_TOOLS
    }
    save()
    assert result["bazel"] == "bazel 9.3.1"
    assert result["build_essential"].startswith("installed ")
    assert result["native_gcc"] == "x86_64-linux-gnu"
    assert all(v is None for v in result["foreign_packages"].values())
    assert all(v is None for v in result["foreign_tools"].values())

    arm = result["arm64"]
    try:
        runtime.phase("arm64", "aarch64", "aarch64", 183, arm, save)
    finally:
        save()
    observed = {(runtime.native_owner(owner), kind, arch)
                for owner, kind, arch in arm.get("matched_native_actions", [])}
    expected = {(family, kind, arch) for family in ("vdso", "stub")
                for kind in ("CppCompile", "CppLink") for arch in ("x86_64", "aarch64")}
    result["actual_compilers_and_links"] = sorted(observed)
    result["verified"] = bool(
        arm.get("verified") and arm["compiler_flag"]["true"] and expected <= observed
    )
    save()
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
