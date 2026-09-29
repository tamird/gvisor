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

"""Assembles the source tree published on the Go branch."""

import argparse
import pathlib
import stat
import zipfile


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--gopath", required=True, type=pathlib.Path)
  parser.add_argument("--output", required=True, type=pathlib.Path)
  parser.add_argument("--go-mod", required=True, type=pathlib.Path)
  parser.add_argument("files", nargs="+", type=pathlib.Path)
  args = parser.parse_args()

  module = next(
      line.split()[1]
      for line in args.go_mod.read_text().splitlines()
      if line.startswith("module ")
  )
  prefix = f"src/{module}/"
  with zipfile.ZipFile(args.gopath) as archive:
    files: dict[str, bytes] = {
        entry.filename.removeprefix(prefix): archive.read(entry)
        for entry in archive.infolist()
        if entry.filename.startswith(prefix) and not entry.is_dir()
    }
  if not files:
    raise ValueError(f"GOPATH archive contains no sources for {module}")
  # These sources are absent from go_path, which consumes libraries rather
  # than binaries to avoid exporting multiple variants of generated files.
  files.update(
      (path.relative_to(args.go_mod.parent).as_posix(), path.read_bytes())
      for path in args.files
  )
  files["README.md"] = b"""# gVisor

This branch is a synthetic branch, containing only Go sources, that is
compatible with standard Go tools. See the master branch for authoritative
sources and tests.
"""
  with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as out:
    for name, content in sorted(files.items()):
      # ZipInfo supplies a fixed timestamp; the published source tree has no
      # executable files, including sources inherited from the GOPATH archive.
      entry = zipfile.ZipInfo(name)
      entry.external_attr = (stat.S_IFREG | 0o644) << 16
      entry.compress_type = zipfile.ZIP_DEFLATED
      out.writestr(entry, content)


if __name__ == "__main__":
  main()
