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

"""Compile an instrumented Linux kernel for the existing native syscall tests."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


def main() -> None:
    match sys.argv[1:]:
        case [config_name]:
            pass
        case _:
            raise SystemExit("Expected one generated configuration path")
    config = json.loads(Path(config_name).read_text())
    root = Path.cwd()
    evidence = root / config["evidence"]
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kcov-kernel-") as temporary:
        work = Path(temporary)
        source = work / "source"
        sysroot = work / "sysroot"
        for directory, archive, options in (
            (source, config["source"], ["--strip-components=1"]),
            (sysroot, config["sysroot"], []),
        ):
            directory.mkdir()
            subprocess.run(
                [str(root / config["tar"]), "-xf", str(root / archive),
                 "-C", str(directory), "--no-same-owner", *options],
                env=config["tar_env"], check=True,
            )
        tools = work / "tools"
        tools.mkdir()

        def wrapper(name: str, command: list[str]) -> Path:
            path = tools / name
            path.write_text("#!/bin/sh\nset -eu\nexec " + shlex.join(command) + ' "$@"\n')
            path.chmod(0o755)
            return path

        libraries = ":".join(str(sysroot / name) for name in (
            "lib/x86_64-linux-gnu", "usr/lib/x86_64-linux-gnu"))
        loader = sysroot / "lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
        for name in ("bc", "bison", "flex", "m4", "perl", "pkg-config"):
            wrapper(name, [str(loader), "--inhibit-cache", "--library-path", libraries,
                           str(sysroot / "usr/bin" / name)])
        for name, path in config["llvm"].items():
            command = [str(root / path)]
            if name == "clang":
                command += ["--no-default-config", "-resource-dir=" + str(root / config["resource_dir"])]
            wrapper(name, command)
        wrapper("hostcc", [
            str(tools / "clang"), "--target=x86_64-linux-gnu",
            "--sysroot=" + str(sysroot),
            "--gcc-install-dir=" + str(sysroot / "usr/lib/gcc/x86_64-linux-gnu/11"),
        ])
        env = {
            **os.environ,
            "PATH": str(tools) + ":/usr/bin:/bin",
            "BISON_PKGDATADIR": str(sysroot / "usr/share/bison"),
            "M4": str(tools / "m4"),
            "PERL5LIB": ":".join(str(path) for pattern in (
                "usr/lib/*/perl-base", "usr/lib/*/perl/*", "usr/share/perl/*", "usr/share/perl5"
            ) for path in sysroot.glob(pattern) if path.is_dir()),
            "PKG_CONFIG_SYSROOT_DIR": str(sysroot),
            "PKG_CONFIG_LIBDIR": str(sysroot / "usr/lib/x86_64-linux-gnu/pkgconfig"),
            "PKG_CONFIG_PATH": "",
            "LD_LIBRARY_PATH": libraries,
            # Kbuild records these values in the image; keep them independent
            # of the remote worker and wall clock.
            # https://docs.kernel.org/kbuild/reproducible-builds.html
            "KBUILD_BUILD_TIMESTAMP": "2026-09-13 00:00:00 UTC",
            "KBUILD_BUILD_USER": "gvisor",
            "KBUILD_BUILD_HOST": "rbe",
            "KBUILD_BUILD_VERSION": "1",
            "LC_ALL": "C",
        }
        build = work / "build"
        build.mkdir()
        make = [str(root / config["make"]), "-C", str(source), "O=" + str(build),
                "ARCH=x86_64", "LLVM=1", "HOSTCC=" + str(tools / "hostcc"),
                "HOSTLDFLAGS=--ld-path=" + str(root / config["llvm"]["ld.lld"])]

        def run(command: list[str], name: str) -> None:
            log = evidence / (name + ".log")
            with log.open("wb") as output:
                result = subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT)
            if result.returncode:
                sys.stderr.write(log.read_text(errors="replace"))
                raise subprocess.CalledProcessError(result.returncode, command)

        run([*make, "x86_64_defconfig"], "defconfig")
        configure = [str(source / "scripts/config"), "--file", str(build / ".config")]
        # KCOV needs compiler instrumentation and a mounted debugfs. The other
        # options provide the existing guest's disks, 9p exports and setup.
        for option in (
            "KCOV", "KCOV_INSTRUMENT_ALL", "KCOV_SELFTEST", "DEBUG_FS",
            "VIRTIO_PCI", "VIRTIO_BLK", "EXT4_FS", "DEVTMPFS", "DEVTMPFS_MOUNT",
            "NET_9P", "NET_9P_VIRTIO", "9P_FS", "KVM", "KVM_INTEL", "KVM_AMD",
            "NETFILTER", "NETFILTER_XTABLES", "NF_CONNTRACK", "IP_NF_IPTABLES",
            "IP6_NF_IPTABLES", "NETFILTER_XT_MATCH_CONNTRACK",
        ):
            configure += ["--enable", option]
        configure += ["--disable", "LOCALVERSION_AUTO", "--set-str", "LOCALVERSION", "-gvisor-kcov"]
        run(configure, "configure")
        run([*make, "olddefconfig"], "olddefconfig")
        settings = (build / ".config").read_text().splitlines()
        for option in ("KCOV", "DEBUG_FS"):
            if "CONFIG_" + option + "=y" not in settings:
                raise RuntimeError(f"Kconfig did not enable {option}")
        run([*make, "-j16", "bzImage", "modules"], "build")
        release = subprocess.check_output([*make, "-s", "kernelrelease"], env=env, text=True).strip()
        if release != config["release"]:
            raise RuntimeError(f"Kernel release {release!r} != {config['release']!r}")
        staging = work / "install"
        # The existing guest-image action runs declared depmod after combining
        # the kernel archive and userspace; no host index generation is needed.
        run([*make, "INSTALL_MOD_PATH=" + str(staging), "INSTALL_MOD_STRIP=1",
             "DEPMOD=true", "modules_install"], "install")
        # modules_install creates build/source links to disposable build paths.
        for name in ("build", "source"):
            (staging / "lib/modules" / release / name).unlink(missing_ok=True)
        boot = staging / "boot"
        boot.mkdir()
        shutil.copyfile(build / "arch/x86/boot/bzImage", boot / ("vmlinuz-" + release))
        shutil.copyfile(build / ".config", boot / ("config-" + release))
        shutil.copyfile(build / ".config", evidence / "kernel.config")
        archive = root / config["archive"]
        archive.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(root / config["tar"]), "-cf", str(archive), "-C", str(staging), "."],
                       env=config["tar_env"], check=True)


if __name__ == "__main__":
    main()
