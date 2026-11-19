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

"""Adapt canonical Buildifier JSON diagnostics to a formatter exit status."""

import difflib
import json
from pathlib import Path
import subprocess
import sys

from python.runfiles import runfiles


BUILDIFIER_WARNINGS = (
    "-duplicated-name,-list-append,-native-py,-native-sh-binary,"
    "-native-sh-library,-native-sh-test,-native-proto"
)


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



def main() -> int:
  match sys.argv:
    case [_, tool_path, mode, *files] if mode in ("check", "fix") and files:
      resolver = runfiles.Create()
      if resolver is None or (tool := resolver.Rlocation(tool_path)) is None:
        raise FileNotFoundError(tool_path)
    case _:
      raise ValueError("Expected Buildifier runfile, check/fix mode and files")
  flags = (["--mode=fix", "--lint=fix"] if mode == "fix" else [
      "--mode=check", "--lint=warn", "--format=json",
  ]) + ["--warnings=" + BUILDIFIER_WARNINGS]
  result = subprocess.run(
      [tool, *flags, *files], capture_output=True, text=True, check=False,
  )
  print(result.stderr, file=sys.stderr, end="")
  if result.returncode or mode == "fix":
    print(result.stdout, end="")
    return result.returncode
  success, unformatted = buildifier_result(result.stdout, files)
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
  return int(not success)


if __name__ == "__main__":
  sys.exit(main())
