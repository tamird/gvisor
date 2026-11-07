"""Run the release ELF verifier without native or cross compiler packages."""

import hashlib
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
    status = result.stdout.strip()
    # dpkg-query can know a package name even when it was never installed.
    return None if status == "not-installed" else status


def output(*argv):
    return subprocess.check_output(argv, text=True).strip()


def main():
    arch = os.environ["RELEASE_ARCH"]
    config, cpu, machine = {"arm64": ("aarch64", "aarch64", 183), "amd64": ("x86_64", "k8", 62)}[arch]
    runtime.BAZEL = runtime.q.BAZEL = BAZEL
    result = {
        "source": SOURCE,
        "host": platform.machine(),
        "image_id": os.environ["BUILDER_IMAGE"],
        "scope": "runsc and Sentry built on a compiler-free AMD64 Jammy client",
        "target": arch,
        "roots": runtime.ROOTS,
        "targetResult": {},
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
    packages = output("dpkg-query", "-W", "-f=${binary:Package}=${Version}\n")
    result["packagesSHA256"] = hashlib.sha256(packages.encode()).hexdigest()
    result["client_packages"] = {name: installed_version(name) for name in
                                 ("git", "python3", "ca-certificates", "patch", "zip", "unzip")}
    result["system_compiler_packages"] = {name: installed_version(name) for name in
                                          ("build-essential", "make", "gcc", "g++", "binutils", "clang", "lld", "docker.io")}
    result["system_tools"] = {name: shutil.which(name) for name in
                              ("cc", "c++", "gcc", "g++", "clang", "clang++", "as", "ld", "ld.lld",
                               "ar", "nm", "objcopy", "readelf", "make", "docker")}
    result["foreign_packages"] = {p: installed_version(p) for p in FOREIGN_PACKAGES}
    result["foreign_tools"] = {
        t: shutil.which("aarch64-linux-gnu-" + t) for t in FOREIGN_TOOLS
    }
    save()
    assert result["bazel"] == "bazel 9.3.1"
    assert all(v is None for v in result["system_compiler_packages"].values())
    assert all(v is None for v in result["system_tools"].values())
    assert all(v is None for v in result["foreign_packages"].values())
    assert all(v is None for v in result["foreign_tools"].values())

    arm = result["targetResult"]
    try:
        runtime.phase(arch, config, cpu, machine, arm, save)
    finally:
        save()
    observed = {(runtime.native_owner(owner), kind, arch)
                for owner, kind, arch in arm.get("matched_native_actions", [])}
    expected = {(family, kind, arch) for family in ("vdso", "stub")
                for kind in ("CppCompile", "CppLink") for arch in ("x86_64", "aarch64")}
    result["actual_compilers_and_links"] = sorted(observed)
    result["verified"] = bool(
        arm.get("verified") and arm["compiler_flag"]["true"] and (arch == "amd64" or expected <= observed)
    )
    save()
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
