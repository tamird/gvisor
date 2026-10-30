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

"""Build a wheel and a source distribution from declared project files."""

import gzip
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import tomllib

from build.__main__ import main as build
import tomli_w


def normalize_sdist(source: Path, temporary: Path, epoch: int) -> None:
    """Keep the backend's members, removing ownership and times from the worker."""
    with (
        tarfile.open(source, "r:gz") as original,
        temporary.open("wb") as output,
        gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=epoch) as compressed,
        tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as normalized,
    ):
        for member in original:
            data = original.extractfile(member) if member.isfile() else None
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            member.mtime = epoch
            for field in ("uid", "gid", "uname", "gname", "mtime", "atime", "ctime"):
                member.pax_headers.pop(field, None)
            normalized.addfile(member, data)
    temporary.replace(source)


def main(pyproject: Path, version: str, output: Path, sources: list[Path]) -> None:
    # The backend runs from a different directory than the initial Bazel action.
    os.environ["RUNFILES_DIR"] = str(Path(os.environ["RUNFILES_DIR"]).absolute())
    # The backend copies staged file modes into the source distribution.
    os.umask(0o022)
    output = output.absolute()
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        work = Path(tmp)
        # setuptools reads the user's distutils configuration from HOME. Keep
        # both it and the backend's temporary files within the build action.
        os.environ.update(HOME=tmp, TMPDIR=tmp)
        project = work / "project"
        for source in [pyproject, *sources]:
            destination = project / source.relative_to(pyproject.parent)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        if version:
            staged = project / pyproject.name
            with staged.open("rb") as original:
                metadata = tomllib.load(original)
            metadata["project"]["version"] = version
            with staged.open("wb") as rewritten:
                tomli_w.dump(metadata, rewritten)

        build(["--no-isolation", "--outdir", str(output), str(project)])
        (sdist,) = output.glob("*.tar.gz")
        # setuptools does not honor SOURCE_DATE_EPOCH when producing an sdist.
        # https://github.com/pypa/setuptools/issues/2133
        normalize_sdist(sdist, work / sdist.name, int(os.environ["SOURCE_DATE_EPOCH"]))


if __name__ == "__main__":
    pyproject, version, output, *sources = sys.argv[1:]
    main(Path(pyproject), version, Path(output), [Path(source) for source in sources])
