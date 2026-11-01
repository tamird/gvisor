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

"""Build the SandboxExec source distribution and a wheel from that source."""

import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile


def main():
    backend, project, output, version, *sources = sys.argv[1:]
    sys.path.insert(0, str(Path(backend).resolve()))
    from flit_core import buildapi

    project = Path(project)
    output = Path(output).resolve()
    output.mkdir(exist_ok=True)
    os.umask(0o022)
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        stage = Path(tmp)
        for name in sources:
            source = Path(name)
            target = stage / source.relative_to(project.parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        text = project.read_text()
        if version:
            text = text.replace('version = "0.1.0"', f'version = "{version}"')
        (stage / project.name).write_text(text)
        os.chdir(stage)
        sdist = buildapi.build_sdist(str(output))
        with tarfile.open(output / sdist) as archive:
            archive.extractall(stage / "sdist", filter="data")
        os.chdir(stage / "sdist" / sdist.removesuffix(".tar.gz"))
        buildapi.build_wheel(str(output))


if __name__ == "__main__":
    main()
