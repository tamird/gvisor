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
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from tools.go_export.compile import go_environment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("codeql", "manifest", "output", "sarif"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--language", choices=("go", "javascript", "python", "ruby"), required=True)
    for name in ("go", "goroot", "proxy", "cc", "cxx"):
        parser.add_argument("--" + name, type=Path)
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
        for number, name in enumerate(names):
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(manifest.parent / "files" / str(number), target)
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
                GOFLAGS="-mod=readonly -buildvcs=false",
            )
            (binaries / "go").symlink_to(args.go.absolute())
        env.update(PATH=os.pathsep.join(path), PYTHONDONTWRITEBYTECODE="1")

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
        run("create", [
            str(codeql), "database", "create", str(database), cache,
            "--language=" + args.language, "--source-root=" + str(source),
            "--codescanning-config=" + str(config),
            "--threads=4", "--ram=12288",
        ])
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
