#!/usr/bin/env python3

# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Compiles an exported module without network or ambient build tools."""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import zipfile


# Cgo changes directory before invoking the compiler. Resolve toolchain paths
# from the action's execroot first, as rules_go's builders/env.go does:
# https://github.com/bazel-contrib/rules_go/blob/v0.62.0/go/tools/builders/env.go
_PATH_FLAGS = (
    "-I",
    "-L",
    "-B",
    "-isysroot",
    "-isystem",
    "-internal-isystem",
    "-iquote",
    "-include",
    "-imacros",
    "-gcc-toolchain",
    "--sysroot",
    "-resource-dir",
    "-fsanitize-blacklist",
    "-fsanitize-ignorelist",
    "--warning-suppression-mappings",
    "-idirafter",
    "-isystem-after",
    "--include-directory-after",
    "--ld-path",
)


def absolute_flags(flags: list[str]) -> list[str]:
    result: list[str] = []
    needs_path = False
    for value in flags:
        if needs_path and value != "-Xclang":
            value = str(Path(value).absolute())
            needs_path = False
        elif not needs_path:
            for flag in _PATH_FLAGS:
                if value == flag:
                    needs_path = True
                    break
                if value.startswith(flag + "="):
                    value = flag + "=" + str(Path(value[len(flag) + 1:]).absolute())
                    break
                if flag in ("-I", "-L", "-B") and value.startswith(flag):
                    value = flag + str(Path(value[len(flag):]).absolute())
                    break
        # Linker selection accepts a name such as "lld" or a tool path.
        if value.startswith("-fuse-ld=") and "/" in value:
            value = "-fuse-ld=" + str(Path(value.removeprefix("-fuse-ld=")).absolute())
        result.append(value)
    if needs_path:
        raise ValueError(f"Toolchain option has no path: {flags!r}")
    return result


def go_environment(
    *,
    goroot: Path,
    proxy: Path,
    work: Path,
    cc: Path | None,
    cxx: Path | None,
    cflags: list[str],
    cxxflags: list[str],
    ldflags: list[str],
) -> dict[str, str]:
    """Use declared tools/modules and action-private Go state.

    Disable user configuration, toolchain downloads, VCS and checksum-server
    access: the module_proxy repository already resolves and verifies inputs.
    https://go.dev/ref/mod#environment-variables
    """
    env = dict(
        os.environ,
        GOENV="off",
        GOFLAGS="",
        GOTOOLCHAIN="local",
        GOWORK="off",
        GOTELEMETRY="off",
        GOROOT=str(goroot.absolute()),
        GOMAXPROCS="4",
        GOPROXY=proxy.absolute().as_uri(),
        GOSUMDB="off",
        GOPRIVATE="",
        GONOPROXY="",
        GONOSUMDB="",
        GOVCS="*:off",
        CGO_ENABLED="1" if cc else "0",
        GOCACHE=str(work / "build"),
        GOMODCACHE=str(work / "modules"),
        GOPATH=str(work / "gopath"),
        GOTMPDIR=str(work / "tmp"),
    )
    Path(env["GOTMPDIR"]).mkdir()
    if cc:
        if cxx is None:
            raise ValueError("A C++ compiler is required with the C compiler")
        env.update(
            CC=str(cc.absolute()),
            CXX=str(cxx.absolute()),
            CGO_CFLAGS=shlex.join(absolute_flags(cflags)),
            CGO_CXXFLAGS=shlex.join(absolute_flags(cxxflags)),
            CGO_LDFLAGS=shlex.join(absolute_flags(ldflags)),
        )
    return env


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "go", "goroot", "proxy", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--goos", required=True)
    parser.add_argument("--goarch", required=True)
    parser.add_argument("--cc", type=Path)
    parser.add_argument("--cxx", type=Path)
    for name in ("cflag", "cxxflag", "ldflag"):
        parser.add_argument("--" + name, action="append", default=[])
    parser.add_argument("packages", nargs="+")
    args = parser.parse_args()
    output = args.output.absolute()
    go = args.go.absolute()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        env = go_environment(
            goroot=args.goroot,
            proxy=args.proxy,
            work=work,
            cc=args.cc,
            cxx=args.cxx,
            cflags=args.cflag,
            cxxflags=args.cxxflag,
            ldflags=args.ldflag,
        )
        env.update(GOOS=args.goos, GOARCH=args.goarch)
        source = work / "source"
        source.mkdir()
        with zipfile.ZipFile(args.archive) as archive:
            archive.extractall(source)
        originals = {name: (source / name).read_bytes() for name in ("go.mod", "go.sum")}
        subprocess.run(
            [str(go), "build", "-mod=readonly", "-buildvcs=false", "-p=4", *args.packages],
            cwd=source,
            env=env,
            check=True,
        )
        for name, original in originals.items():
            if (source / name).read_bytes() != original:
                raise ValueError(f"go build changed {name}; update the module metadata first")
    output.write_text(json.dumps({
        "goos": args.goos,
        "goarch": args.goarch,
        "cgo": bool(args.cc),
        "packages": args.packages,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
