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

"""Checks developer-tool failure propagation."""

import contextlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

import clang_tidy

LINT = pathlib.Path(sys.argv.pop(1)).absolute()


class LintTest(unittest.TestCase):

  def setUp(self) -> None:
    super().setUp()
    self.temp = tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"])
    self.addCleanup(self.temp.cleanup)
    self.root = pathlib.Path(self.temp.name)
    self.cache = self.root / "cache"
    self.bin = self.root / "bin"
    self.execroot = self.root / "execroot"
    self.bazel_log = self.root / "bazel.log"
    self.env = dict(os.environ, LINT_CACHE_DIR=str(self.cache))
    self.env.pop("QUALIFICATION_BAZEL", None)
    self.env.pop("QUALIFICATION_BAZEL_OPTIONS_JSON", None)
    self.env["PATH"] = str(self.bin) + os.pathsep + self.env["PATH"]
    self.cache.mkdir()
    self.bin.mkdir()
    self.execroot.mkdir()
    self.versions = dict(
        re.findall(
            r'^declare -r ([A-Z_]+)_VERSION="([^"]+)"$',
            LINT.read_text(),
            re.MULTILINE,
        )
    )
    self.tool(self.bin / "git", "printf 'tracked source\\0'")
    self.tool(self.bin / "curl", "echo unexpected-download >&2; exit 42")
    # Bazel materializes both tools at the execroot-relative paths from cquery.
    self.tool(
        self.bin / "bazel",
        f'echo "$*" >> "{self.bazel_log}"\n'
        'case " $* " in\n'
        f'  *" info "*) echo "{self.execroot}" ;;\n'
        '  *" cquery "*)\n'
        '    case "$*" in\n'
        '      *"@buildifier_prebuilt//:buildifier"*) echo "buildifier buildifier" ;;\n'
        '      *) echo "goimports goimports" ;;\n'
        '    esac ;;\n'
        "esac",
    )
    for name in ("goimports", "clang-format", "buildifier"):
      self.tool(self.formatter(name), "exit 0")

  def tool(self, path: pathlib.Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)

  def formatter(self, name: str) -> pathlib.Path:
    if name in ("goimports", "buildifier"):
      return self.execroot / name
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
    # Exercise plain pipeline, captured output and --fix status handling.
    for args in (
        ("goimports",),
        ("clang-format",),
        ("buildifier",),
        ("--fix", "goimports"),
        ("--fix", "buildifier"),
    ):
      name = args[-1]
      with self.subTest(args=args):
        self.tool(self.formatter(name), "exit 7")
        self.assert_failed(self.lint(*args), name)
        self.tool(self.formatter(name), "exit 0")

  def test_bazel_options(self) -> None:
    result = self.lint(
        "--bazel=bazel --startup=1",
        "--bazel-options=--opt=1 --opt=2",
        "goimports",
    )
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("PASS  goimports", result.stderr)
    commands = [c.split() for c in self.bazel_log.read_text().splitlines()]
    self.assertEqual(
        [c[:4] for c in commands],
        [
            ["--startup=1", command, "--opt=1", "--opt=2"]
            for command in ("build", "info", "cquery")
        ],
    )

  def test_qualification_bazel_options(self) -> None:
    qualified = self.bin / "qualified bazel"
    self.tool(
        qualified,
        "python3 -c 'import json, sys; "
        'f = open(sys.argv[1], "a"); '
        'f.write(json.dumps(sys.argv[2:]) + "\\n")' + "' "
        f'"{self.bazel_log}" "$@"\n'
        'case "$1" in\n'
        f'  info) echo "{self.execroot}" ;;\n'
        '  cquery) echo "goimports goimports" ;;\n'
        "esac",
    )
    options = ["--credential_helper=/physical/helper with spaces", "--keep_going"]
    self.env["QUALIFICATION_BAZEL"] = str(qualified)
    self.env["QUALIFICATION_BAZEL_OPTIONS_JSON"] = json.dumps(options)
    result = self.lint("goimports")
    self.assertEqual(result.returncode, 0, result.stderr)
    commands = [json.loads(line) for line in self.bazel_log.read_text().splitlines()]
    self.assertEqual(
        [command[:3] for command in commands],
        [[command, *options] for command in ("build", "info", "cquery")],
    )

  def test_invalid_qualification_bazel_options(self) -> None:
    self.env["QUALIFICATION_BAZEL"] = str(self.bin / "bazel")
    for options in ({}, [1], ["nul\0argument"]):
      with self.subTest(options=options):
        self.env["QUALIFICATION_BAZEL_OPTIONS_JSON"] = json.dumps(options)
        result = self.lint("goimports")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.bazel_log.exists(), result.stdout + result.stderr)

  def test_compile_commands_bazel_options(self) -> None:
    self.tool(
        self.bin / "bazel",
        f'echo "$*" >> "{self.bazel_log}"\n'
        'case " $* " in *" run "*) echo generator-stopped >&2; exit 7;; esac',
    )
    result = self.lint(
        "--bazel=bazel --startup=1",
        "--bazel-options=--opt=1 --opt=2",
        "clang-tidy",
    )
    self.assert_failed(result, "clang-tidy")
    self.assertIn("generator-stopped", result.stderr)
    (generate,) = [c.split() for c in self.bazel_log.read_text().splitlines()]
    self.assertEqual(generate[:4], ["--startup=1", "run", "--opt=1", "--opt=2"])
    self.assertIn("--platforms=@platforms//host", generate)
    self.assertIn("--bazel-command=bazel", generate)
    self.assertIn("--bazelsupopt=--startup=1", generate)
    self.assertIn("--bazelopt=--opt=1", generate)
    self.assertIn("--bazelopt=--opt=2", generate)
    self.tool(
        self.bin / "bazel",
        f'echo "$*" >> "{self.bazel_log}"\n'
        'case " $* " in\n'
        '  *" run "*) for arg; do case "$arg" in --output=*) '
        'printf generated > "${arg#--output=}";; esac; done;;\n'
        '  *" build "*) echo materialization-stopped >&2; exit 7;;\n'
        "esac",
    )
    self.bazel_log.unlink()
    result = self.lint("clang-tidy")
    self.assert_failed(result, "clang-tidy")
    self.assertIn("materialization-stopped", result.stderr)
    commands = [c.split() for c in self.bazel_log.read_text().splitlines()]
    output = next(arg for arg in commands[0] if arg.startswith("--output="))
    self.assertFalse(pathlib.Path(output.removeprefix("--output=")).exists())
    self.assertFalse((LINT.parent.parent / "compile_commands.json").exists())

  def test_clang_tidy_process_failure(self) -> None:
    tool = self.bin / "clang-tidy"
    self.tool(tool, "echo driver-stdout; echo driver-stderr >&2; exit 7")
    source = pathlib.Path("source.cc")
    analyzed, findings, errors = clang_tidy._analyze(
        str(tool), self.root, self.root / ".clang-tidy", self.root, source
    )
    self.assertEqual(analyzed, source)
    self.assertEqual(findings, [])
    self.assertIn("source.cc: clang-tidy exited with status 7", errors)
    self.assertIn("driver-stdout", errors)
    self.assertIn("driver-stderr", errors)

  def test_bazel_failure(self) -> None:
    self.tool(self.bin / "bazel", "echo bazel-build-failed >&2; exit 7")
    result = self.lint("goimports")
    self.assert_failed(result, "goimports")
    self.assertIn("bazel-build-failed", result.stderr)

    self.tool(
        self.bin / "bazel",
        f'echo "$*" >> "{self.bazel_log}"\n'
        'case "$*" in *"@io_bazel_rules_go//go"*) exit 0;; esac\nexit 7',
    )
    result = subprocess.run(
        ["bash", str(LINT.with_name("devcontainer-init.sh"))],
        env=self.env,
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
    self.assertEqual(len(self.bazel_log.read_text().splitlines()), 2)

  def test_diagnostics_and_continuation(self) -> None:
    self.tool(
        self.formatter("clang-format"), "echo formatter-crashed >&2; exit 7"
    )
    result = self.lint("clang-format", "buildifier")
    self.assert_failed(result, "clang-format")
    self.assertIn("formatter-crashed", result.stderr)
    self.assertIn("PASS  buildifier", result.stderr)

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


class ClangTidySourcesTest(unittest.TestCase):

  def test_compilation_directory(self) -> None:
    with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as temp:
      root = pathlib.Path(temp)
      workspace, execroot = root / "workspace", root / "execroot"
      workspace.mkdir()
      execroot.mkdir()
      relative, absolute = workspace / "relative.cc", workspace / "absolute.cc"
      relative.touch()
      absolute.touch()
      (execroot / "relative.cc").symlink_to(relative)
      outside = root / "external.cc"
      outside.touch()
      database = workspace / "compile_commands.json"
      database.write_text(json.dumps([
          {"directory": str(execroot), "file": "relative.cc"},
          {"directory": str(execroot), "file": "relative.cc"},
          {"directory": str(execroot), "file": str(absolute)},
          {"directory": str(root), "file": "external.cc"},
      ]))
      with contextlib.chdir(root):
        self.assertEqual(
            clang_tidy._sources(database, workspace),
            [pathlib.Path("absolute.cc"), pathlib.Path("relative.cc")],
        )


if __name__ == "__main__":
  unittest.main()
