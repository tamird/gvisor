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

"""Exercise the configured fixers on temporary source files."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from python.runfiles import runfiles


RESOLVER = runfiles.Create()
if RESOLVER is None:
  raise RuntimeError("Bazel runfiles are required")


def resolve(path: str) -> str:
  result = RESOLVER.Rlocation(path)
  if result is None:
    raise FileNotFoundError(path)
  return result


match sys.argv:
  case [program, goimports_fix, goimports, buildifier_fix, buildifier, *test_args]:
    GOIMPORTS_FIX, GOIMPORTS, BUILDIFIER_FIX, BUILDIFIER = map(
        resolve, (goimports_fix, goimports, buildifier_fix, buildifier)
    )
    sys.argv = [program, *test_args]
  case _:
    raise ValueError("Expected four configured formatter paths")


class FixerTest(unittest.TestCase):

  def test_fixers(self) -> None:
    cases = (
        ("fixture.go", "package fixture\nfunc answer( )int{return 42}\n",
         "package fixture\n\nfunc answer() int { return 42 }\n",
         GOIMPORTS_FIX, GOIMPORTS, ["-format-only", "-d"]),
        ("BUILD", 'exports_files( [ "fixture.txt" ] )\n',
         'exports_files(["fixture.txt"])\n',
         BUILDIFIER_FIX, BUILDIFIER, ["--mode=check", "--format=json"]),
    )
    for name, before, after, fixer, tool, flags in cases:
      with self.subTest(name=name), tempfile.TemporaryDirectory(
          dir=os.environ["TEST_TMPDIR"]
      ) as directory:
        root = Path(directory)
        source = root / name
        source.write_text(before)
        manifest = root / "sources.json"
        manifest.write_text(json.dumps([name]))
        env = dict(os.environ, BUILD_WORKSPACE_DIRECTORY=directory)

        def check() -> bool:
          result = subprocess.run(
              [tool, *flags, name], cwd=root, env=env,
              capture_output=True, text=True, check=True,
          )
          if name == "BUILD":
            match json.loads(result.stdout):
              case {"success": bool(success)}:
                return success
              case unexpected:
                self.fail(f"Invalid Buildifier response: {unexpected!r}")
          return not result.stdout

        self.assertFalse(check(), "Unformatted fixture was accepted")
        result = subprocess.run(
            [fixer, "--check=" + ("buildifier" if name == "BUILD" else "goimports"),
             "--tool=" + tool, "--sources=" + str(manifest), "--fix"], env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(source.read_text(), after)
        self.assertTrue(check(), "Fixed fixture was rejected")


if __name__ == "__main__":
  unittest.main()
