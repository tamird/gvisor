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

"""Resolves the action's compilation database and invokes native clang-tidy."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main() -> int:
  tool, database, config, report = sys.argv[1:]
  # clang-tidy's compilation-database matcher ignores relative file paths.
  # Resolve them only here, when the action's actual execroot is known.
  match json.loads(Path(database).read_text()):
    case [{"file": str(source), "arguments": list(arguments)}]:
      assert all(isinstance(arg, str) for arg in arguments), arguments
    case value:
      raise ValueError(f"Expected one compilation command, got {value!r}")
  source_path = str(Path(source).resolve())
  command = {
      "directory": str(Path.cwd()),
      "file": source_path,
      "arguments": arguments,
  }
  with tempfile.TemporaryDirectory() as directory:
    Path(directory, "compile_commands.json").write_text(json.dumps([command]))
    with Path(report).open("w") as output:
      result = subprocess.run(
          [
              tool,
              "-p",
              directory,
              f"--config-file={config}",
              "--quiet",
              "--warnings-as-errors=*",
              source_path,
          ],
          stdout=output,
          stderr=subprocess.STDOUT,
          check=False,
      )
  if result.returncode:
    with Path(report).open() as output:
      shutil.copyfileobj(output, sys.stderr)
  return result.returncode


if __name__ == "__main__":
  raise SystemExit(main())
