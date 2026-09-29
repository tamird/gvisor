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

"""Check distribution payloads and import the installed wheel in isolation."""

import argparse
from email.parser import BytesParser
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile
from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename
from packaging.version import Version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--pyproject", type=Path, required=True)
    parser.add_argument("--version-file", type=Path, required=True)
    parser.add_argument("sources", type=Path, nargs="+")
    args = parser.parse_args()
    configuration = tomllib.loads(args.pyproject.read_text())
    project = configuration["project"]
    name = canonicalize_name(project["name"])
    version = Version(args.version_file.read_text().strip() or project["version"])
    packages = configuration["tool"]["setuptools"]["packages"]
    expected_sources = {
        source.relative_to(args.pyproject.parent).as_posix(): source.read_bytes()
        for source in args.sources
        if any(
            source.is_relative_to(args.pyproject.parent / p.replace(".", "/"))
            for p in packages
        )
    }
    assert expected_sources, "No declared package sources"
    (wheel,) = args.dist.glob("*.whl")
    (sdist,) = args.dist.glob("*.tar.gz")
    assert set(args.dist.iterdir()) == {wheel, sdist}, list(args.dist.iterdir())
    wheel_name, wheel_version, _, tags = parse_wheel_filename(wheel.name)
    assert (wheel_name, wheel_version) == (name, version), wheel.name
    assert {str(tag) for tag in tags} == {"py3-none-any"}, tags
    assert parse_sdist_filename(sdist.name) == (name, version), sdist.name

    def check_metadata(contents: bytes) -> None:
        metadata = BytesParser().parsebytes(contents)
        assert canonicalize_name(metadata["Name"]) == name, metadata["Name"]
        assert Version(metadata["Version"]) == version, metadata["Version"]
        for field, expected in (
            ("Summary", project["description"]),
            ("Requires-Python", project["requires-python"]),
            ("License", project["license"]["text"]),
        ):
            assert metadata[field] == expected, (field, metadata[field], expected)
        assert sorted(metadata.get_all("Classifier", [])) == sorted(project["classifiers"])
        assert sorted(metadata.get_all("Project-URL", [])) == sorted(
            f"{label}, {url}" for label, url in project["urls"].items()
        )
        assert metadata.get_all("Requires-Dist", []) == project["dependencies"]
        readme = (args.pyproject.parent / project["readme"]).read_text()
        assert metadata.get_payload().rstrip() == readme.rstrip(), "README metadata differs"

    with tarfile.open(sdist) as archive:
        members = {member.name: member for member in archive if member.isfile()}
        (root,) = {Path(path).parts[0] for path in members}

        def read_sdist(path: str) -> bytes:
            source = archive.extractfile(members[f"{root}/{path}"])
            assert source is not None, path
            with source:
                return source.read()

        for path, contents in expected_sources.items():
            assert read_sdist(path) == contents, path
        assert read_sdist(project["readme"]) == (
            args.pyproject.parent / project["readme"]
        ).read_bytes()
        staged = tomllib.loads(read_sdist("pyproject.toml").decode())
        assert Version(staged["project"].pop("version")) == version
        configuration["project"].pop("version")
        assert staged == configuration, "Distribution changed project metadata"
        check_metadata(read_sdist("PKG-INFO"))

    with zipfile.ZipFile(wheel) as archive:
        files = {path for path in archive.namelist() if not path.endswith("/")}
        payload = {
            path for path in files
            if not path.split("/")[0].endswith(".dist-info")
        }
        assert payload == set(expected_sources), payload
        for path, contents in expected_sources.items():
            assert archive.read(path) == contents, path
        (metadata_path,) = (path for path in files if path.endswith(".dist-info/METADATA"))
        check_metadata(archive.read(metadata_path))

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        site = root / "site-packages"
        destination = SchemeDictionaryDestination(
            scheme_dict={
                "purelib": str(site),
                "platlib": str(site),
                "scripts": str(root / "bin"),
                "headers": str(root / "include"),
                "data": str(root / "data"),
            },
            interpreter=sys.executable,
            script_kind="posix",
        )
        with WheelFile.open(wheel) as source:
            source.validate_record(validate_contents=True)
            install(source, destination, {})
        # -I -S ignores PYTHONPATH, the working directory and all site packages. Only
        # the installed wheel and standard library can satisfy these imports.
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import importlib, pathlib, sys\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "for package in sys.argv[2:]:\n"
                "    module = importlib.import_module(package)\n"
                "    pathlib.Path(module.__file__).relative_to(sys.argv[1])\n",
                str(site),
                *packages,
            ],
            cwd=root,
            check=True,
        )
    print(f"Verified {wheel.name}, {sdist.name}, metadata, RECORD and isolated installation")


if __name__ == "__main__":
    main()
