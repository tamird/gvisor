#!/usr/bin/env python3
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

"""Analyze declared checkout contents without downloading tools or uploading results."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from typing import TypedDict
import zipfile

from tools.go_export.compile import go_environment


# go/build's fileListForExt treats these as package inputs alongside .go files.
# https://github.com/golang/go/blob/022c86361/src/go/build/build.go#L1085
GO_BUILD_SUFFIXES = {
    ".go", ".c", ".cc", ".cpp", ".cxx", ".m", ".h", ".hh", ".hpp",
    ".hxx", ".f", ".F", ".for", ".f90", ".s", ".S", ".sx", ".swig",
    ".swigcxx", ".syso",
}


class GoSourceView(TypedDict):
    added: dict[str, str]
    omitted: dict[str, str]
    packages: dict[str, list[str]]
    relocated: dict[str, list[str]]


def prepare_go_packages(source: Path, archive_paths: list[Path], source_map: Path) -> GoSourceView:
    """Use canonical file membership only for packages defined by the export."""
    added: dict[str, str] = {}
    omitted: dict[str, str] = {}
    packages: dict[str, list[str]] = {}
    files: dict[PurePosixPath, bytes] = {}
    relocated: dict[str, list[str]] = {}
    for archive_path in archive_paths:
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                # The exported README describes its synthetic Go branch. Keep the
                # checkout's original; module files are checked like other sources.
                if entry.is_dir() or entry.filename == "README.md":
                    continue
                name = PurePosixPath(entry.filename)
                if name.is_absolute() or ".." in name.parts:
                    raise ValueError(f"Invalid Go source archive path: {entry.filename}")
                content = archive.read(entry)
                target = source / name
                if target.exists() and target.read_bytes() != content:
                    raise ValueError(f"Exported Go source differs from tracked source: {name}")
                if name in files:
                    if files[name] != content:
                        raise ValueError(f"Conflicting exported Go source: {name}")
                    continue
                files[name] = content
                if name.suffix == ".go":
                    packages.setdefault(str(name.parent), []).append(name.name)

    # Validate declared moves before changing any source. Identical bytes alone
    # do not establish ownership: only the producer's source map permits moving
    # a tracked file out of its original mixed-package directory.
    for entry in json.loads(source_map.read_text()):
        original = PurePosixPath(entry["source"])
        destination = PurePosixPath(entry["path"])
        if original.is_absolute() or ".." in original.parts or destination.is_absolute() or ".." in destination.parts:
            raise ValueError(f"Invalid Go source mapping: {entry}")
        content = files[destination]
        if (source / original).read_bytes() != content:
            raise ValueError(f"Relocated Go source differs from tracked source: {original}")
        if original != destination:
            relocated.setdefault(str(original), []).append(str(destination))

    # A template and its generated instance can declare the same identifiers.
    # Keep the producer's package membership instead of combining both. Other
    # checkout packages remain intact. This also removes raw C/assembly owned by
    # separate Bazel targets that Go would otherwise treat as package inputs.
    # Omissions are recorded, not guessed to be templates or other-platform files.
    for package in packages:
        for target in (source / package).glob("*"):
            if not target.is_file() or target.suffix not in GO_BUILD_SUFFIXES:
                continue
            name = PurePosixPath(target.relative_to(source).as_posix())
            if name not in files:
                omitted[str(name)] = hashlib.sha256(target.read_bytes()).hexdigest()
                target.unlink()
    for original in relocated:
        target = source / original
        if PurePosixPath(original) not in files and original not in omitted:
            omitted[original] = hashlib.sha256(target.read_bytes()).hexdigest()
            target.unlink()
    for name, content in files.items():
        target = source / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            added[str(name)] = hashlib.sha256(content).hexdigest()
    return {"added": added, "omitted": omitted, "packages": packages, "relocated": relocated}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("codeql", "manifest", "output", "sarif"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--language", choices=("go", "javascript", "python", "ruby"), required=True)
    for name in ("go", "goroot", "proxy", "go-mod", "go-sum", "go-source-map", "cc", "cxx"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--go-sources", type=Path, action="append", default=[])
    for name in ("cflag", "cxxflag", "ldflag"):
        parser.add_argument("--" + name, action="append", default=[])
    args = parser.parse_args()
    codeql = args.codeql.absolute()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=True)
    sarif = args.sarif.absolute()
    sarif.parent.mkdir(parents=True, exist_ok=True)
    manifest = args.manifest.absolute()
    names = json.loads(manifest.read_text())

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        source = work / "source"
        source.mkdir()
        for name in names:
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(manifest.parent / "files" / (name + ".source"), target)
        shutil.copyfile(manifest, output / "sources.json")

        # The Python extractor discovers its interpreter through PATH. Use the
        # declared py_binary runtime, not a Python installation in the worker.
        binaries = work / "bin"
        binaries.mkdir()
        for name in ("python", "python3"):
            (binaries / name).symlink_to(sys.executable)
        path = [str(binaries)] + [str(Path(entry).absolute()) for entry in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep)]
        env = dict(os.environ)
        if args.language == "go":
            # Bazel owns generation and package membership. Analyzing only
            # the export would omit other packages from the checkout.
            shutil.copyfile(args.go_source_map, output / "go-source-map.json")
            view = prepare_go_packages(source, args.go_sources, args.go_source_map)
            (output / "go-source-view.json").write_text(json.dumps(view, indent=2) + "\n")
            # Use the declared full-checkout dependency profile without
            # changing the checked-in module that belongs to Go source export.
            # https://go.dev/ref/mod#build-commands (the -modfile option)
            go_mod = work / "analysis.mod"
            shutil.copyfile(args.go_mod, go_mod)
            shutil.copyfile(args.go_sum, go_mod.with_suffix(".sum"))
            shutil.copyfile(go_mod, output / "input-analysis.mod")
            shutil.copyfile(go_mod.with_suffix(".sum"), output / "input-analysis.sum")
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
            env.update(
                CODEQL_EXTRACTOR_GO_BUILD_COMMAND=":",
                # Package loading may update this private derived profile.
                # The declared file proxy still bounds available dependencies.
                GOFLAGS=f"-modfile={go_mod} -mod=mod -buildvcs=false",
            )
            (binaries / "go").symlink_to(args.go.absolute())
        env.update(PATH=os.pathsep.join(path), PYTHONDONTWRITEBYTECODE="1")
        if args.language == "python":
            # The Bazel launcher enables PYTHONSAFEPATH, but CodeQL's index.py
            # imports the sibling python_tracer module. Restore normal script
            # imports for the extractor subprocess.
            # https://docs.python.org/3.11/using/cmdline.html#envvar-PYTHONSAFEPATH
            env.pop("PYTHONSAFEPATH", None)

        def run(name: str, command: list[str]) -> None:
            with (output / (name + ".log")).open("w") as log:
                result = subprocess.run(command, cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT)
            print((output / (name + ".log")).read_text(), end="", flush=True)
            result.check_returncode()

        cache = "--common-caches=" + str(work / "codeql-cache")
        run("version", [str(codeql), "version", "--format=json"])
        if args.language == "go":
            run("dependencies", [str(args.go.absolute()), "mod", "download"])
        database = output / "database"
        config = output / "config.yml"
        config.write_text("{}\n")
        try:
            run("create", [
                str(codeql), "database", "create", str(database), cache,
                "--language=" + args.language, "--source-root=" + str(source),
                "--codescanning-config=" + str(config),
                "--threads=4", "--ram=12288",
            ])
        finally:
            if args.language == "go":
                # The Go autobuilder runs tidy even with BUILD_COMMAND=":".
                # Retain its effective profile as well as the declared input.
                shutil.copyfile(go_mod, output / "analysis.mod")
                shutil.copyfile(go_mod.with_suffix(".sum"), output / "analysis.sum")
        # With no query customization, the Action and CLI use the bundled
        # language pack's default code-scanning suite. Keep source coverage and
        # extraction diagnostics alongside findings; success is not proof that
        # every generated Go package was available to the extractor.
        run("analyze", [
            str(codeql), "database", "analyze", str(database), cache,
            "--format=sarif-latest", "--output=" + str(sarif),
            "--threads=4", "--ram=12288", "--print-diagnostics-summary",
            "--print-metrics-summary", "--sarif-add-baseline-file-info",
            "--sarif-group-rules-by-pack", "--sarif-include-query-help=always",
            "--sublanguage-file-coverage", "--sarif-include-diagnostics",
            "--sarif-codescanning-config=" + str(config),
            "--sarif-category=/language:" + args.language,
        ])


if __name__ == "__main__":
    main()
