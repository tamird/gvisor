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

"""Build the canonical sdist and then a wheel from that sdist, offline."""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

import build
from packaging.version import Version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyproject", type=Path, required=True)
    parser.add_argument("--version-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("sources", type=Path, nargs="+")
    args = parser.parse_args()
    source_root = args.pyproject.parent
    output = args.output.resolve()
    # Backend subprocesses must see the same declared libraries as this tool,
    # even after changing to the staged project directory.
    python_path = os.pathsep.join(str(Path(p).resolve()) for p in sys.path)
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        project = work / "project"
        for source in [args.pyproject, *args.sources]:
            destination = project / source.relative_to(source_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        if version := args.version_file.read_text().strip():
            pyproject = project / "pyproject.toml"
            contents, replacements = re.subn(
                r'^version = ".*"$',
                f'version = "{Version(version)}"',
                pyproject.read_text(),
                flags=re.MULTILINE,
            )
            if replacements != 1:
                raise ValueError("Expected one literal project version in pyproject.toml")
            pyproject.write_text(contents)
        home = work / "home"
        home.mkdir()

        def run_backend(
            command: list[str], cwd: str, extra_environ: dict[str, str]
        ) -> None:
            subprocess.run(
                command,
                cwd=cwd,
                env=os.environ
                | {
                    "HOME": str(home),
                    "PYTHONPATH": python_path,
                    "PYTHONNOUSERSITE": "1",
                    "PIP_NO_INDEX": "1",
                    # Wheel's standard timestamp input; setuptools' sdist
                    # writer does not promise reproducible archive metadata.
                    "SOURCE_DATE_EPOCH": "315532800",
                }
                | extra_environ,
                check=True,
            )

        builder = build.ProjectBuilder(project, runner=run_backend)
        if missing := builder.check_dependencies("sdist"):
            raise ValueError(f"Undeclared sdist build requirements: {missing}")
        sdist = builder.build("sdist", output)
        unpacked = work / "sdist"
        with tarfile.open(sdist) as archive:
            archive.extractall(unpacked, filter="data")
        (sdist_project,) = unpacked.iterdir()
        builder = build.ProjectBuilder(sdist_project, runner=run_backend)
        if missing := builder.check_dependencies("wheel"):
            raise ValueError(f"Undeclared wheel build requirements: {missing}")
        builder.build("wheel", output)


if __name__ == "__main__":
    main()
