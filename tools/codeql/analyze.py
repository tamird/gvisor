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

"""Analyze declared checkout contents without downloads or result uploads."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from tools.codeql.go_analysis import extract


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("codeql", "manifest", "output", "sarif"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--language", choices=("go", "javascript", "python", "ruby"), required=True)
    for name in ("go", "goroot", "extractor", "driver"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    codeql = args.codeql.absolute()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=True)
    sarif = args.sarif.absolute()
    sarif.parent.mkdir(parents=True, exist_ok=True)
    manifest = args.manifest.absolute()

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        env = dict(os.environ)
        if args.language == "go":
            if any(getattr(args, name) is None for name in ("go", "goroot", "extractor", "driver")):
                raise ValueError("Go analysis requires its declared SDK, extractor and package driver")
            database = extract(args)
            source = Path.cwd()
        else:
            source = work / "source"
            source.mkdir()
            for name in json.loads(manifest.read_text()):
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(manifest.parent / "files" / (name + ".source"), target)
            shutil.copyfile(manifest, output / "sources.json")
            # CodeQL discovers Python through PATH; use the declared runtime.
            binaries = work / "bin"
            binaries.mkdir()
            for name in ("python", "python3"):
                (binaries / name).symlink_to(sys.executable)
            path = [str(binaries)] + [str(Path(entry).absolute()) for entry in env.get("PATH", "/usr/bin:/bin").split(os.pathsep)]
            env.update(PATH=os.pathsep.join(path), PYTHONDONTWRITEBYTECODE="1")
            if args.language == "python":
                # index.py imports its sibling python_tracer module.
                env.pop("PYTHONSAFEPATH", None)
            database = output / "database"

        def run(name: str, command: list[str]) -> None:
            with (output / (name + ".log")).open("w") as log:
                result = subprocess.run(command, cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT)
            print((output / (name + ".log")).read_text(), end="", flush=True)
            result.check_returncode()

        cache = "--common-caches=" + str(work / "codeql-cache")
        config = output / "config.yml"
        config.write_text("{}\n")
        run("version", [str(codeql), "version", "--format=json"])
        if args.language != "go":
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
