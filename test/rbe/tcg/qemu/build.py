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

"""Build a matched QEMU pair; executed only as a declared remote Bazel action."""

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
    with tempfile.TemporaryDirectory(prefix="qemu-rcsc-") as temporary:
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

        for name in ("ninja", "pkg-config", "patch"):
            wrapper(name, [str(loader), "--inhibit-cache", "--library-path", libraries, str(sysroot / "usr/bin" / name)])
        # Preserve the repository's selected LLVM binaries and resource headers,
        # with the same complete GNU sysroot boundary used by Syzkaller.
        # No cd: Meson's relative input/output arguments belong to its own CWD.
        flags = [
            "--no-default-config", "--target=x86_64-linux-gnu",
            "--sysroot=" + str(sysroot),
            "--gcc-install-dir=" + str(sysroot / "usr/lib/gcc/x86_64-linux-gnu/11"),
            "-resource-dir=" + str(root / config["resource_dir"]),
            "--ld-path=" + str(root / config["linker"]),
            "--rtlib=libgcc", "--unwindlib=libgcc", "--stdlib=libstdc++",
        ]
        cc = wrapper("cc", [str(root / config["cc"]), *flags])
        cxx = wrapper("cxx", [str(root / config["cxx"]), *flags])
        # QEMU6.2 requires --meson=internal when an explicit Python is selected.
        # Keep the declared Meson package intact; only its entry point is placed
        # in that expected source layout, using Bazel's existing Python runtime.
        (source / "meson").mkdir()
        shutil.copyfile(sysroot / "usr/bin/meson", source / "meson/meson.py")
        env = {
            **os.environ,
            "PATH": str(tools) + ":/usr/bin:/bin",
            "PYTHONPATH": str(sysroot / "usr/lib/python3/dist-packages"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PKG_CONFIG": str(tools / "pkg-config"),
            "PKG_CONFIG_SYSROOT_DIR": str(sysroot),
            "PKG_CONFIG_LIBDIR": str(sysroot / "usr/lib/x86_64-linux-gnu/pkgconfig") + ":" + str(sysroot / "usr/share/pkgconfig"),
            "PKG_CONFIG_PATH": "",
            "LD_LIBRARY_PATH": libraries,
            "SOURCE_DATE_EPOCH": "1638835200",
            "LC_ALL": "C",
        }
        # Apply the complete, unchanged distro series with the declared GNU
        # tool, before either build. Reject fuzz and unsupported series options.
        ubuntu_patches = []
        for line in (source / "debian/patches/series").read_text().splitlines():
            match line.split("#", 1)[0].split():
                case []:
                    continue
                case [name]:
                    patch = source / "debian/patches" / name
                case _:
                    raise ValueError(f"Unsupported quilt series options: {line}")
            log = evidence / "ubuntu-patches" / (name + ".log")
            log.parent.mkdir(parents=True, exist_ok=True)
            run_logged([str(tools / "patch"), "--batch", "--forward", "--fuzz=0", "--no-backup-if-mismatch", "-p1", "-i", str(patch)], source, env, log)
            ubuntu_patches.append({"path": name, "sha256": sha256(patch)})
        (evidence / "ubuntu-patches.json").write_text(json.dumps(ubuntu_patches, indent=2) + "\n")
        options = [
            "/bin/sh", str(source / "configure"),
            "--without-default-features", "--target-list=aarch64-softmmu",
            "--enable-system", "--disable-user", "--enable-tcg",
            "--enable-virtfs", "--enable-attr", "--enable-cap-ng",
            "--enable-fdt=system",
            "--disable-docs", "--disable-tools", "--disable-werror",
            "--disable-install-blobs", "--disable-strip",
            "--with-git-submodules=ignore", "--with-pkgversion=RCsc-pair",
            "--cc=" + str(cc), "--cxx=" + str(cxx),
            "--ar=" + str(root / config["ar"]),
            "--python=" + sys.executable, "--meson=internal",
            "--ninja=" + str(tools / "ninja"),
            "--make=" + str(root / config["make"]),
            "--prefix=/usr", "--libdir=/usr/lib/x86_64-linux-gnu",
        ]
        (evidence / "configure-arguments.json").write_text(json.dumps(options, indent=2) + "\n")
        manifests = {}
        build = work / "build"
        for arm in ("before", "after"):
            if arm == "after":
                subprocess.run([str(tools / "patch"), "--batch", "--forward", "--fuzz=0", "--no-backup-if-mismatch", "-p1", "-i", str(root / config["patch"])], cwd=source, env=env, check=True)
                shutil.rmtree(build)
            build.mkdir()
            arm_evidence = evidence / arm
            arm_evidence.mkdir()
            manifests[arm] = {str(path.relative_to(source)): sha256(path) for path in source.rglob("*") if path.is_file()}
            run_logged(options, build, env, arm_evidence / "configure.log")
            run_logged([str(root / config["make"]), "-j8", "qemu-system-aarch64"], build, env, arm_evidence / "build.log")
            output = root / config[arm]
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(build / "qemu-system-aarch64", output)
            output.chmod(0o755)
            for name in ("config-host.mak", "config-host.h", "config-meson.cross", "meson-info/intro-buildoptions.json"):
                path = build / name
                if name in ("config-host.mak", "config-host.h") and not path.is_file():
                    raise RuntimeError(f"QEMU omitted required configuration: {name}")
                if path.exists():
                    destination = arm_evidence / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)
            run_logged([str(loader), "--inhibit-cache", "--library-path", libraries, str(output), "--version"], build, env, arm_evidence / "version.txt")
            (arm_evidence / "source-manifest.json").write_text(json.dumps(manifests[arm], sort_keys=True) + "\n")
        changed = {path for path in manifests["before"] | manifests["after"] if manifests["before"].get(path) != manifests["after"].get(path)}
        if changed != {"target/arm/translate-a64.c", "target/arm/translate.c"}:
            raise RuntimeError(f"Unexpected QEMU source delta: {sorted(changed)}")
        for name in ("config-host.mak", "config-host.h", "config-meson.cross"):
            before = evidence / "before" / name
            after = evidence / "after" / name
            if name in ("config-host.mak", "config-host.h") and not (before.is_file() and after.is_file()):
                raise RuntimeError(f"QEMU omitted required configuration: {name}")
            if before.exists() != after.exists() or (before.exists() and before.read_bytes() != after.read_bytes()):
                raise RuntimeError(f"QEMU configuration changed between arms: {name}")
        (evidence / "pair.json").write_text(json.dumps({
            "patchSHA256": sha256(root / config["patch"]),
            "changedSources": sorted(changed),
            "outputs": {arm: sha256(root / config[arm]) for arm in ("before", "after")},
            "compilerFlags": flags,
        }, indent=2) + "\n")


if __name__ == "__main__":
    main()
