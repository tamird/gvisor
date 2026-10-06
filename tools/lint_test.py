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
    temporary = tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"])
    self.addCleanup(temporary.cleanup)
    self.root = pathlib.Path(temporary.name)
    self.calls = self.root / "calls"
    self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"],
                    LINT_CALLS=str(self.calls))
    for name in ("bazel", "make"):
      tool = self.root / name
      tool.write_text("#!/bin/sh\n" +
                      'printf "%s\\n" "$*" >> "$LINT_CALLS"\n' +
                      'case "$*" in *gofmt*) exit 7;; esac\n')
      tool.chmod(0o755)

  def lint(self, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(LINT), *args], env=self.env,
                          capture_output=True, text=True, check=False)

  def test_optional_checks_after_failure(self) -> None:
    result = self.lint("gofmt", "spelling", "actions", "clang-tidy")
    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
    calls = self.calls.read_text().splitlines()
    self.assertEqual(calls, [
        "test --keep_going //tools/lint:gofmt //tools/lint:spelling",
        "test OPTIONS=--enable_runfiles TARGETS=//:github_actions_test",
        "build --config=lint-cc",
    ])

  def test_fix_continues_and_excludes_nonfixable_checks(self) -> None:
    result = self.lint("--fix", "gofmt", "spelling", "buildifier", "actions")
    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
    self.assertEqual(self.calls.read_text().splitlines(), [
        "run //tools/lint:gofmt_fix", "run //tools/lint:buildifier_fix"])

  def test_rejects_invalid_selection_before_running(self) -> None:
    for args in [("gofmt", "unknown"), ("--fix", "spelling")]:
      with self.subTest(args=args):
        result = self.lint(*args)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.calls.exists(), "invalid selection invoked Bazel")


if __name__ == "__main__":
  unittest.main()
