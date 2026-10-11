# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Build the patched ARM emulator as a declared remote Bazel action."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_logged(command: list[str], cwd: Path, env: dict[str, str], log: Path) -> None:
    with log.open("wb") as output:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        # Failed actions need their actual configure/compiler error in Bazel's
        # stderr too; failed output trees are not guaranteed to be downloaded.
        sys.stderr.write(log.read_text(errors="replace"))
        for name in ("config.log", "meson-logs/meson-log.txt"):
            diagnostic = cwd / name
            if diagnostic.is_file():
                sys.stderr.write(diagnostic.read_text(errors="replace"))
        raise subprocess.CalledProcessError(result.returncode, command)


def main() -> None:
    match sys.argv[1:]:
        case [config_name]:
            pass
        case _:
            raise SystemExit("Expected one generated configuration path")
    config = json.loads(Path(config_name).read_text())
    root = Path.cwd()
    evidence = (root / config["evidence"]).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qemu-system-") as temporary:
        work = Path(temporary)
        source = work / "source"
        source_root = Path(config["source_root"])
        for name in config["sources"]:
            path = Path(name)
            destination = source / path.relative_to(source_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / path, destination)
            shutil.copymode(root / path, destination)
        sysroot = work / "sysroot"
        sysroot.mkdir()
        subprocess.run(
            [str(root / config["tar"]), "-xf", str(root / config["sysroot"]),
             "-C", str(sysroot), "--no-same-owner"],
            env=config["tar_env"], check=True,
        )
        loader = sysroot / "lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
        libraries = ":".join(str(sysroot / name) for name in ("lib/x86_64-linux-gnu", "usr/lib/x86_64-linux-gnu"))
        tools = work / "tools"
        tools.mkdir()

        def wrapper(name: str, command: list[str]) -> Path:
            path = tools / name
            path.write_text("#!/bin/sh\nset -eu\nexec " + shlex.join(command) + ' "$@"\n')
            path.chmod(0o755)
            return path

        for name in ("ninja", "pkg-config", "patch", "strings"):
            wrapper(name, [str(loader), "--inhibit-cache", "--library-path", libraries, str(sysroot / "usr/bin" / name)])
        # Some QEMU helpers are invoked directly through env python3 instead
        # of configure's explicit --python interpreter.
        wrapper("python3", [sys.executable])
        # Preserve the repository's selected LLVM binaries and resource headers,
        # with the same complete GNU sysroot boundary used by Syzkaller.
        # No cd: Meson's relative input/output arguments belong to its own CWD.
        flags = [
            "--no-default-config", "--target=x86_64-linux-gnu",
            "--sysroot=" + str(sysroot),
            "--gcc-install-dir=" + str(sysroot / "usr/lib/gcc/x86_64-linux-gnu/11"),
            "-resource-dir=" + str(root / config["resource_dir"]),
        ]
        link_flags = [
            "--ld-path=" + str(root / config["linker"]),
            "--rtlib=libgcc", "--unwindlib=libgcc",
        ]
        cxx_flags = ["--stdlib=libstdc++"]
        # These wrappers serve both compile-only probes and links. Scope Clang's
        # unused handling to our link-only choices; caller flags follow the
        # closing marker and retain all of their ordinary warning checks.
        link_selection = ["--start-no-unused-arguments", *link_flags, "--end-no-unused-arguments"]
        cc = wrapper("cc", [str(root / config["cc"]), *flags, *link_selection])
        cxx = wrapper("cxx", [str(root / config["cxx"]), *flags, *cxx_flags, *link_selection])
        env = {
            **os.environ,
            "PATH": str(tools) + ":/usr/bin:/bin",
            # Bazel's Python launcher enables safe-path mode. Declare QEMU's
            # sibling generator packages explicitly instead of relying on the
            # interpreter to prepend each script directory implicitly.
            "PYTHONPATH": os.pathsep.join([str(source / "scripts"), *[str(Path(path).resolve()) for path in sys.path if path]]),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INDEX": "1",
            # QEMU selects these tools from the environment, then writes
            # them into config-host.mak and Meson's native configuration.
            "AR": str(root / config["ar"]),
            "NM": str(root / config["nm"]),
            "RANLIB": str(root / config["ranlib"]),
            "OBJCOPY": str(root / config["objcopy"]),
            "STRIP": str(root / config["strip"]),
            "LD": str(root / config["linker"]),
            "PKG_CONFIG": str(tools / "pkg-config"),
            "PKG_CONFIG_SYSROOT_DIR": str(sysroot),
            "PKG_CONFIG_LIBDIR": str(sysroot / "usr/lib/x86_64-linux-gnu/pkgconfig") + ":" + str(sysroot / "usr/share/pkgconfig"),
            "PKG_CONFIG_PATH": "",
            "LD_LIBRARY_PATH": libraries,
            "SOURCE_DATE_EPOCH": "1638835200",
            "LC_ALL": "C",
        }
        run_logged([str(tools / "patch"), "--batch", "--forward", "--fuzz=0", "--no-backup-if-mismatch", "-p1", "-i", str(root / config["patch"])], source, env, evidence / "patch.log")
        options = [
            "/bin/sh", str(source / "configure"),
            "--without-default-features", "--target-list=aarch64-softmmu",
            "--enable-system", "--disable-user", "--enable-tcg",
            "--enable-virtfs", "--enable-attr", "--enable-cap-ng",
            "--enable-fdt=system",
            "--disable-docs", "--disable-tools", "--disable-werror",
            "--disable-install-blobs", "--disable-strip",
            "--disable-download", "--disable-rust", "--with-pkgversion=gvisor-rcsc",
            "--cc=" + str(cc), "--cxx=" + str(cxx),
            "--python=" + sys.executable,
            "--ninja=" + str(tools / "ninja"),
            "--make=" + str(root / config["make"]),
            "--prefix=/usr", "--libdir=/usr/lib/x86_64-linux-gnu",
        ]
        (evidence / "configure-arguments.json").write_text(json.dumps(options, indent=2) + "\n")
        build = work / "build"
        build.mkdir()
        run_logged(options, build, env, evidence / "configure.log")
        run_logged([str(root / config["make"]), "-j8", "qemu-system-aarch64"], build, env, evidence / "build.log")
        output = root / config["binary"]
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(build / "qemu-system-aarch64", output)
        output.chmod(0o755)
        for name in ("config-host.mak", "config-host.h", "config-meson.cross", "meson-info/intro-buildoptions.json"):
            path = build / name
            if name in ("config-host.mak", "config-host.h") and not path.is_file():
                raise RuntimeError(f"QEMU omitted required configuration: {name}")
            if path.exists():
                destination = evidence / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
        run_logged([str(loader), "--inhibit-cache", "--library-path", libraries, str(output), "--version"], build, env, evidence / "version.txt")
        (evidence / "build.json").write_text(json.dumps({
            "patchSHA256": sha256(root / config["patch"]),
            "binarySHA256": sha256(output),
            "compilerFlags": flags,
            "cxxOnlyFlags": cxx_flags,
            "linkerFlags": link_flags,
            "injectedLinkSelection": link_selection,
        }, indent=2) + "\n")



if __name__ == "__main__":
    main()
