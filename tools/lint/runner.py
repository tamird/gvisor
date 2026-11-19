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

"""Run source format checks against declared indexed files."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile

from python.runfiles import runfiles


def manifest(path: Path) -> list[str]:
  value: object = json.loads(path.read_text())
  if not isinstance(value, list) or not value:
    raise ValueError(f"{path}: expected a nonempty array of source paths")
  paths: list[str] = []
  for name in value:
    if not isinstance(name, str) or not name:
      raise ValueError(f"{path}: expected a nonempty source path, got {name!r}")
    source = PurePosixPath(name)
    if source.is_absolute() or ".." in source.parts:
      raise ValueError(f"{path}: source path is not project-relative: {name!r}")
    paths.append(name)
  if len(set(paths)) != len(paths):
    raise ValueError(f"{path}: duplicate source paths")
  return paths


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--command", required=True)
  parser.add_argument("--sources", required=True)
  parser.add_argument("--fix", action=argparse.BooleanOptionalAction, default=False)
  args = parser.parse_args()
  resolver = runfiles.Create()
  if resolver is None:
    raise RuntimeError("Bazel runfiles are required")
  command = resolver.Rlocation(args.command)
  if command is None:
    raise FileNotFoundError(args.command)
  source_path = resolver.Rlocation(args.sources)
  if source_path is None:
    raise FileNotFoundError(args.sources)
  source = Path(source_path)
  files = manifest(source)
  with tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR")) as directory:
    root = Path(os.environ["BUILD_WORKSPACE_DIRECTORY"]) if args.fix else Path(directory) / "workspace"
    if not args.fix:
      # Project-relative paths preserve Buildifier's file-type detection.
      for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source.parent / "files" / (name + ".source"), target)
    parameter_file = Path(directory) / "sources.txt"
    parameter_file.write_text("\n".join(files) + "\n")
    env = dict(os.environ, BUILD_WORKSPACE_DIRECTORY=str(root),
               BUILD_WORKING_DIRECTORY=str(root))
    result = subprocess.run(
        [command, "@" + str(parameter_file)], cwd=root, env=env, check=False,
    )
    if result.returncode:
      print("Run `make lint-fix` to reformat these files.", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
  sys.exit(main())
