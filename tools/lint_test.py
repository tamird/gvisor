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

"""Checks failure propagation through the public lint entrypoint."""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

LINT = pathlib.Path(sys.argv.pop(1)).absolute()


class LintTest(unittest.TestCase):

  def setUp(self) -> None:
    super().setUp()
    self.temp = tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"])
    self.addCleanup(self.temp.cleanup)
    self.root = pathlib.Path(self.temp.name)
    self.bin = self.root / "bin"
    self.env = os.environ.copy()
    self.env["PATH"] = str(self.bin) + os.pathsep + self.env["PATH"]
    self.bin.mkdir()
    self.tool(self.bin / "git", 'if [ "$2" = -z ]; then printf "tracked.c\\0"; fi')
    self.bazel_ok()

  def tool(self, path: pathlib.Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)

  def bazel_ok(self) -> None:
    self.tool(self.bin / "bazel", 'if [ "$1" = query ]; then echo //:tracked.c; fi')

  def lint(self, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(LINT), *args],
        env=self.env,
        capture_output=True,
        text=True,
        check=False,
    )

  def assert_failed(
      self, result: subprocess.CompletedProcess[str], name: str
  ) -> None:
    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
    self.assertIn(f"FAIL  {name}", result.stderr)
    self.assertNotIn("lint: all checks passed.", result.stderr)

  def test_formatter_status(self) -> None:
    names = ("goimports", "clang-format", "buildifier")
    success = self.lint(*names)
    self.assertEqual(success.returncode, 0, success.stderr)
    for name in names:
      self.assertIn(f"PASS  {name}", success.stderr)
    # Exercise plain pipeline, captured output and --fix status handling.
    for args in (
        ("goimports",),
        ("clang-format",),
        ("buildifier",),
        ("--fix", "goimports"),
    ):
      name = args[-1]
      with self.subTest(args=args):
        self.tool(self.bin / "bazel", "exit 7")
        self.assert_failed(self.lint(*args), name)
        self.bazel_ok()

  def test_diagnostics_and_continuation(self) -> None:
    self.tool(
        self.bin / "bazel",
        'case "$*" in *buildifier*) exit 0;; esac; echo formatter-crashed >&2; exit 7',
    )
    result = self.lint("clang-format", "buildifier")
    self.assert_failed(result, "clang-format")
    self.assertIn("formatter-crashed", result.stderr)
    self.assertIn("PASS  buildifier", result.stderr)

  def test_failed_file_selection(self) -> None:
    # The last spelling selection succeeds; it must not hide an earlier
    # failed list operation in the same pipeline.
    self.tool(self.bin / "git", 'case "$4" in "*.md") exit 7;; esac; exit 0')
    self.assert_failed(self.lint("spelling"), "spelling")


if __name__ == "__main__":
  unittest.main()
