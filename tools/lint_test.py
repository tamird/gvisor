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
import re
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
    self.cache = self.root / "cache"
    self.bin = self.root / "bin"
    self.bazel_log = self.root / "bazel.log"
    self.home_log = self.root / "home.log"
    self.env = dict(os.environ, LINT_CACHE_DIR=str(self.cache))
    self.env["PATH"] = str(self.bin) + os.pathsep + self.env["PATH"]
    self.cache.mkdir()
    self.bin.mkdir()
    self.versions = dict(
        re.findall(
            r'^declare -r ([A-Z_]+)_VERSION="([^"]+)"$',
            LINT.read_text(),
            re.MULTILINE,
        )
    )
    self.tool(self.bin / "git", "printf 'tracked source\\0'")
    self.tool(self.bin / "curl", "echo unexpected-download >&2; exit 42")
    self.bazel()
    self.tool(self.formatter("clang-format"), "exit 0")

  def bazel(self, failure: str | None = None) -> None:
    body = (
        f'printf "%s\\n" "$*" >> "{self.bazel_log}"\n'
        f'printf "%s" "$HOME" > "{self.home_log}"\n'
    )
    if failure:
      body += f'case "$*" in *"{failure}"*) echo bazel-failed >&2; exit 7;; esac\n'
    self.tool(self.bin / "bazel", body)

  def tool(self, path: pathlib.Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)

  def formatter(self, name: str) -> pathlib.Path:
    version = self.versions[name.upper().replace("-", "_")]
    return self.cache / f"{name}-{version}"

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
    self.assertEqual(self.bazel_log.read_text().splitlines(), [
        "test --test_output=errors //tools/lint:goimports",
        "test --test_output=errors //tools/lint:buildifier",
    ])
    self.tool(self.formatter("clang-format"), "exit 7")
    self.assert_failed(self.lint("clang-format"), "clang-format")

  def test_bazel_check_and_fix_failures(self) -> None:
    for args in (
        ("goimports",), ("buildifier",),
        ("--fix", "goimports"), ("--fix", "buildifier"),
    ):
      with self.subTest(args=args):
        self.bazel("//tools/lint:" + args[-1])
        result = self.lint(*args)
        self.assert_failed(result, args[-1])
        self.assertIn("bazel-failed", result.stderr)

  def test_bazel_options_and_home(self) -> None:
    for fix in (False, True):
      with self.subTest(fix=fix):
        self.bazel_log.unlink(missing_ok=True)
        args = [
            "--bazel=bazel --startup=1",
            "--bazel-options=--opt=1 --opt=2",
        ]
        if fix:
          args.append("--fix")
        result = self.lint(*args, "goimports", "buildifier")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [c.split() for c in self.bazel_log.read_text().splitlines()]
        self.assertEqual(commands, [
            ["--startup=1", "run" if fix else "test", "--opt=1", "--opt=2"]
            + ([] if fix else ["--test_output=errors"])
            + ["//tools/lint:" + name + ("_fix" if fix else "")]
            for name in ("goimports", "buildifier")
        ])
        self.assertEqual(self.home_log.read_text(), os.environ.get("HOME", ""))

  def test_diagnostics_and_continuation(self) -> None:
    self.tool(
        self.formatter("clang-format"), "echo formatter-crashed >&2; exit 7"
    )
    result = self.lint("clang-format", "buildifier")
    self.assert_failed(result, "clang-format")
    self.assertIn("formatter-crashed", result.stderr)
    self.assertIn("PASS  buildifier", result.stderr)
    self.tool(self.formatter("clang-format"), "exit 0")
    self.bazel("//tools/lint:goimports")
    result = self.lint("goimports", "clang-format", "buildifier")
    self.assert_failed(result, "goimports")
    self.assertIn("PASS  clang-format", result.stderr)
    self.assertIn("PASS  buildifier", result.stderr)

  def test_fix_excludes_nonfixable_checks(self) -> None:
    result = self.lint("--fix", "goimports", "spelling", "buildifier", "actions")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(self.bazel_log.read_text().splitlines(), [
        "run //tools/lint:goimports_fix",
        "run //tools/lint:buildifier_fix",
    ])

  def test_rejects_invalid_selection(self) -> None:
    for args in (("goimports", "unknown"), ("--fix", "spelling")):
      with self.subTest(args=args):
        result = self.lint(*args)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.bazel_log.exists(), "invalid selection invoked Bazel")

  def test_installer_failure(self) -> None:
    # The remaining native formatter must not make a stale cache executable
    # after an installer failure in a command substitution.
    self.formatter("clang-format").chmod(0o644)
    result = self.lint("clang-format")
    self.assert_failed(result, "clang-format")
    self.assertIn("unexpected-download", result.stderr)
    self.assertEqual(self.formatter("clang-format").stat().st_mode & 0o111, 0)
    # A failed temporary-file allocation must not be masked by local assignment.
    self.tool(self.bin / "mktemp", "echo temporary-file-failed >&2; exit 7")
    result = self.lint("clang-format", "goimports")
    self.assert_failed(result, "clang-format")
    self.assertIn("temporary-file-failed", result.stderr)
    self.assertNotIn("unexpected-download", result.stderr)
    self.assertIn("PASS  goimports", result.stderr)

  def test_failed_file_selection(self) -> None:
    # The last spelling selection succeeds; it must not hide an earlier
    # failed list operation in the same pipeline.
    self.tool(self.bin / "git", 'case "$4" in "*.md") exit 7;; esac; exit 0')
    (self.cache / f"codespell-{self.versions['CODESPELL']}").mkdir()
    self.tool(self.bin / "python3", "exit 0")
    self.assert_failed(self.lint("spelling"), "spelling")


if __name__ == "__main__":
  unittest.main()
