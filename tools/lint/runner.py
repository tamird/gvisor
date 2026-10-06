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

"""Run declared source checkers against one shared indexed-file selection."""

import argparse
import difflib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

from python.runfiles import runfiles


BUILDIFIER_WARNINGS = (
    "-duplicated-name,-list-append,-native-py,-native-sh-binary,"
    "-native-sh-library,-native-sh-test,-native-proto"
)


def manifest(path: pathlib.Path) -> list[str]:
  value = json.loads(path.read_text())
  if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
    raise ValueError(f"{path}: expected an array of source paths")
  return value


def formatted_diff(name: str, formatted: str) -> None:
  original = pathlib.Path(name).read_text()
  sys.stdout.writelines(difflib.unified_diff(
      original.splitlines(keepends=True), formatted.splitlines(keepends=True),
      fromfile=name, tofile=name + " (formatted)"))


def check(name: str, tool: str, files: list[str], fix: bool) -> int:
  flags = {
      "gofmt": ["-w", "-l"] if fix else ["-l"],
      "clang-format": ["-i"] if fix else ["--dry-run", "-Werror"],
      "buildifier": ["--mode=fix", "--lint=fix"] if fix else ["--mode=check", "--lint=warn"],
      "cpplint": ["--quiet", "--filter=-,+build/include_order,+build/c++11"],
      "spelling": ["--config", "tools/.codespellrc"],
  }[name]
  if name == "buildifier":
    flags.append("--warnings=" + BUILDIFIER_WARNINGS)
  if name == "clang-format" and not pathlib.Path(".clang-format").is_file():
    raise FileNotFoundError("lint: .clang-format is missing from the repository root")
  if name == "spelling" and not pathlib.Path("tools/.codespellrc").is_file():
    raise FileNotFoundError("lint: tools/.codespellrc is missing")
  env = os.environ.copy()
  if name == "cpplint":
    env["PYTHONWARNINGS"] = "ignore::DeprecationWarning"
  failed = False
  for offset in range(0, len(files), 128):
    batch = files[offset:offset + 128]
    result = subprocess.run([tool, *flags, *batch], env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            check=False)
    output = result.stdout
    print(output, end="")
    failed |= result.returncode != 0
    if fix:
      continue
    if name == "gofmt" and output:
      failed = True
      # Ask gofmt for its own diff without splitting filenames from -l output.
      diff = subprocess.run([tool, "-d", *batch], check=False)
      failed |= diff.returncode != 0
    elif name == "buildifier" and output:
      # --lint=warn may report warnings while returning success.
      failed = True
      for file in re.findall(r"^(.*) # reformat$", output, re.MULTILINE):
        with pathlib.Path(file).open() as source:
          formatted = subprocess.run([tool, "-path=" + file], stdin=source,
                                     capture_output=True, text=True, check=False)
        if formatted.returncode:
          print(formatted.stderr, file=sys.stderr, end="")
        else:
          formatted_diff(file, formatted.stdout)
    elif name == "clang-format" and result.returncode:
      names = set(re.findall(r"^(.*):[0-9]+:[0-9]+: (?:warning|error):", output, re.MULTILINE))
      for file in sorted(names):
        if file not in batch:
          continue
        formatted = subprocess.run([tool, file], capture_output=True,
                                   text=True, check=False)
        if formatted.returncode:
          print(formatted.stderr, file=sys.stderr, end="")
        else:
          formatted_diff(file, formatted.stdout)
  if failed and name in ("gofmt", "clang-format", "buildifier") and not fix:
    print("Run `make lint-fix` to reformat these files.", file=sys.stderr)
  return int(failed)


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--check", required=True, choices=(
      "gofmt", "clang-format", "buildifier", "cpplint", "spelling"))
  parser.add_argument("--tool", required=True)
  parser.add_argument("--sources", action="append", required=True)
  parser.add_argument("--configs", action="append", default=[])
  parser.add_argument("--fix", action="store_true")
  args = parser.parse_args()
  resolver = runfiles.Create()
  if resolver is None:
    raise RuntimeError("Bazel runfiles are required")
  tool = resolver.Rlocation(args.tool)
  if tool is None:
    raise FileNotFoundError(args.tool)
  sources = [pathlib.Path(resolver.Rlocation(path)) for path in args.sources]
  configs = [pathlib.Path(resolver.Rlocation(path)) for path in args.configs]
  files = [name for source in sources for name in manifest(source)]
  if args.fix:
    os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
    return check(args.check, tool, files, True)
  # Keep project-relative arguments and parent configs for tool discovery and
  # codespell's skip patterns. Each test declares only its relevant subset.
  with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as directory:
    root = pathlib.Path(directory)
    for source in sources + configs:
      for name in manifest(source):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source.parent / "files" / (name + ".source"), target)
    os.chdir(root)
    return check(args.check, tool, files, False)


if __name__ == "__main__":
  sys.exit(main())
