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

import subprocess
import sys

from python.runfiles import runfiles

from tools.lint.runner import BUILDIFIER_WARNINGS, buildifier_result


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
  success, _ = buildifier_result(result.stdout, files)
  return int(not success)


if __name__ == "__main__":
  sys.exit(main())
