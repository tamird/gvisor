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
import difflib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile

from python.runfiles import runfiles


BUILDIFIER_WARNINGS = (
    "-duplicated-name,-list-append,-native-py,-native-sh-binary,"
    "-native-sh-library,-native-sh-test,-native-proto"
)


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


def buildifier_result(output: str, files: list[str]) -> tuple[bool, list[str]]:
  value: object = json.loads(output)
  if not isinstance(value, dict):
    raise ValueError("Buildifier: expected a diagnostics object")
  success = value.get("success")
  if not isinstance(success, bool):
    raise ValueError("Buildifier: expected a boolean success field")
  diagnostics = value.get("files")
  if not isinstance(diagnostics, list):
    raise ValueError("Buildifier: expected a files array")
  names: list[str] = []
  unformatted: list[str] = []
  findings = False
  for entry in diagnostics:
    if not isinstance(entry, dict):
      raise ValueError(f"Buildifier: invalid file diagnostic: {entry!r}")
    filename = entry.get("filename")
    if not isinstance(filename, str):
      raise ValueError(f"Buildifier: invalid filename: {filename!r}")
    formatted = entry.get("formatted")
    valid = entry.get("valid")
    warnings = entry.get("warnings")
    if not isinstance(formatted, bool) or not isinstance(valid, bool):
      raise ValueError(f"Buildifier: invalid formatting status for {filename}")
    if not isinstance(warnings, list) or not all(isinstance(w, dict) for w in warnings):
      raise ValueError(f"Buildifier: invalid warnings for {filename}")
    if formatted and not valid:
      raise ValueError(f"Buildifier: invalid file marked formatted: {filename}")
    names.append(filename)
    findings |= not formatted or not valid or bool(warnings)
    if valid and not formatted:
      unformatted.append(filename)
  if sorted(names) != sorted(files):
    raise ValueError(f"Buildifier: files = {names!r}, want {files!r}")
  if success == findings:
    raise ValueError("Buildifier: success disagrees with file diagnostics")
  print(json.dumps(value, indent=2))
  return success, unformatted


def check(name: str, tool: str, files: list[str], fix: bool) -> int:
  flags = {
      "goimports": ["-format-only", "-w", "-l"] if fix else ["-format-only", "-d"],
      "buildifier": (["--mode=fix", "--lint=fix"] if fix else [
          "--mode=check", "--lint=warn", "--format=json",
      ]) + ["--warnings=" + BUILDIFIER_WARNINGS],
  }[name]
  failed = False
  for offset in range(0, len(files), 128):
    batch = files[offset:offset + 128]
    result = subprocess.run(
        [tool, *flags, *batch], capture_output=True, text=True, check=False,
    )
    print(result.stderr, file=sys.stderr, end="")
    if fix:
      print(result.stdout, end="")
      failed |= result.returncode != 0
      continue
    if name == "goimports":
      print(result.stdout, end="")
      failed |= result.returncode != 0 or bool(result.stdout)
      continue
    if result.returncode:
      print(result.stdout, end="")
      failed = True
      continue
    success, unformatted = buildifier_result(result.stdout, batch)
    failed |= not success
    for file in unformatted:
      with Path(file).open() as source:
        formatted = subprocess.run(
            [tool, "-path=" + file], stdin=source,
            capture_output=True, text=True, check=False,
        )
      if formatted.returncode:
        print(formatted.stderr, file=sys.stderr, end="")
      else:
        sys.stdout.writelines(difflib.unified_diff(
            Path(file).read_text().splitlines(keepends=True),
            formatted.stdout.splitlines(keepends=True),
            fromfile=file, tofile=file + " (formatted)",
        ))
  if failed:
    print("Run `make lint-fix` to reformat these files.", file=sys.stderr)
  return int(failed)


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--check", required=True, choices=("goimports", "buildifier"))
  parser.add_argument("--tool", required=True)
  parser.add_argument("--sources", required=True)
  parser.add_argument("--fix", action="store_true")
  args = parser.parse_args()
  resolver = runfiles.Create()
  if resolver is None:
    raise RuntimeError("Bazel runfiles are required")
  tool = resolver.Rlocation(args.tool)
  if tool is None:
    raise FileNotFoundError(args.tool)
  source_path = resolver.Rlocation(args.sources)
  if source_path is None:
    raise FileNotFoundError(args.sources)
  source = Path(source_path)
  files = manifest(source)
  if args.fix:
    os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
    return check(args.check, tool, files, True)
  # Project-relative paths preserve Buildifier's file-type detection.
  with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as directory:
    root = Path(directory)
    for name in files:
      target = root / name
      target.parent.mkdir(parents=True, exist_ok=True)
      shutil.copyfile(source.parent / "files" / (name + ".source"), target)
    os.chdir(root)
    return check(args.check, tool, files, False)


if __name__ == "__main__":
  sys.exit(main())
