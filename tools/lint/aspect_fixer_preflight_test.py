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

"""Check and fix real indexed exports through the Aspect formatters."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from python.runfiles import runfiles


resolver = runfiles.Create()
if resolver is None:
  raise RuntimeError("Bazel runfiles are required")


def resolve(path: str) -> str:
  result = resolver.Rlocation(path)
  if result is None:
    raise FileNotFoundError(path)
  return result


match sys.argv:
  case [program, checker_path, fixer_path, go, build, module, bzl, *test_args]:
    CHECKER, FIXER = map(resolve, (checker_path, fixer_path))
    SOURCES = list(map(resolve, (go, build, module, bzl)))
    sys.argv = [program, *test_args]
  case _:
    raise ValueError("Expected configured Aspect commands and four exports")


class AspectFixerTest(unittest.TestCase):

  def test_reject_fix_and_accept(self) -> None:
    cases = (
        ("pkg/tcpip/seqnum/seqnum.go", "package seqnum", "package    seqnum"),
        ("tools/buildbuddy/BUILD", "name =", "name="),
        ("MODULE.bazel", "module(", "module( "),
        ("tools/lint/defs.bzl", "load(", "load( "),
    )
    with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as directory:
      root = Path(directory)
      env = dict(os.environ, BUILD_WORKSPACE_DIRECTORY=directory,
                 BUILD_WORKING_DIRECTORY=directory)
      originals = []
      for (name, _, _), source in zip(cases, SOURCES, strict=True):
        clean = Path(source).read_text()
        originals.append(clean)
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(clean)
      names = [name for name, _, _ in cases]

      def run(binary: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [binary, *names], cwd=root, env=env,
            capture_output=True, text=True, check=False,
        )

      result = run(CHECKER)
      self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
      for (name, before, after), clean in zip(cases, originals, strict=True):
        with self.subTest(name=name):
          self.assertIn(before, clean)
          (root / name).write_text(clean.replace(before, after, 1))
          result = run(CHECKER)
          self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
          result = run(FIXER)
          self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
          self.assertEqual((root / name).read_text(), clean)
          result = run(CHECKER)
          self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
  unittest.main()
