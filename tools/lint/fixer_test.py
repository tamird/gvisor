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
  case [
      program, go_fix, bazel_fix, checker, formatter,
      go, build, module, bzl, *test_args,
  ]:
    GO_FIX, BAZEL_FIX, CHECKER, FORMATTER = map(
        resolve, (go_fix, bazel_fix, checker, formatter)
    )
    SOURCES = list(map(resolve, (go, build, module, bzl)))
    sys.argv = [program, *test_args]
  case _:
    raise ValueError("Expected configured fixers, checker and four exports")


class FixerTest(unittest.TestCase):

  def test_fixers(self) -> None:
    cases = (
        ("pkg/tcpip/seqnum/seqnum.go", "package seqnum", "package    seqnum", 0, GO_FIX),
        ("tools/buildbuddy/BUILD", "name =", "name=", 1, BAZEL_FIX),
        ("MODULE.bazel", "module(", "module( ", 2, BAZEL_FIX),
        ("tools/lint/defs.bzl", "load(", "load( ", 3, BAZEL_FIX),
        ("space dir/file.go", "package seqnum", "package    seqnum", 0, GO_FIX),
        ("literal[ab].go", "package seqnum", "package    seqnum", 0, GO_FIX),
    )
    for name, before, after, source_index, fixer in cases:
      with self.subTest(name=name), tempfile.TemporaryDirectory(
          prefix="format workspace ", dir=os.environ["TEST_TMPDIR"]
      ) as directory:
        root = Path(directory)
        source = root / name
        source.parent.mkdir(parents=True, exist_ok=True)
        original = Path(SOURCES[source_index]).read_text()
        self.assertIn(before, original)
        source.write_text(original.replace(before, after, 1))
        unselected = root / "literala.go"
        unselected.write_text("package    unselected\n")
        manifest = root / "sources.json"
        manifest.write_text(json.dumps([name]))
        mirrored = root / "files" / (name + ".source")
        mirrored.parent.mkdir(parents=True, exist_ok=True)
        subdirectory = root / "invoking subdirectory"
        subdirectory.mkdir()
        env = dict(os.environ, BUILD_WORKSPACE_DIRECTORY=directory,
                   BUILD_WORKING_DIRECTORY=str(subdirectory))

        def check() -> subprocess.CompletedProcess[str]:
          mirrored.write_text(source.read_text())
          return subprocess.run(
              [fixer, "--sources=" + str(manifest), "--command=" + CHECKER,
               "--no-fix"], cwd=subdirectory, env=env,
              capture_output=True, text=True, check=False,
          )

        result = check()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(source.read_text(), original.replace(before, after, 1))
        if fixer == BAZEL_FIX:
          self.assertIn("(formatted)", result.stdout + result.stderr)
        result = subprocess.run(
            [fixer, "--command=" + FORMATTER,
             "--sources=" + str(manifest), "--fix"],
            cwd=subdirectory, env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(source.read_text(), original)
        result = check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(unselected.read_text(), "package    unselected\n")

  def test_reject_newline_before_fixing(self) -> None:
    with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as directory:
      root = Path(directory)
      original = "package    fixture\n"
      names = ("a.go", "b.go", "a.go\nb.go")
      for name in names:
        (root / name).write_text(original)
      manifest = root / "sources.json"
      manifest.write_text(json.dumps(["a.go\nb.go"]))
      env = dict(os.environ, BUILD_WORKSPACE_DIRECTORY=directory,
                 BUILD_WORKING_DIRECTORY=directory)
      result = subprocess.run(
          [GO_FIX, "--command=" + FORMATTER,
           "--sources=" + str(manifest), "--fix"], cwd=root, env=env,
          capture_output=True, text=True, check=False,
      )
      self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
      self.assertIn("source path contains a newline", result.stderr)
      for name in names:
        self.assertEqual((root / name).read_text(), original)


if __name__ == "__main__":
  unittest.main()
