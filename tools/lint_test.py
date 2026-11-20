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
import shutil
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
    # goimports is built with Bazel, then found at the execroot-relative path
    # that cquery prints.
    self.tool(
        self.bin / "bazel",
        f'echo "$*" >> "{self.bazel_log}"\n'
        'case " $* " in\n'
        f'  *" info "*) echo "{self.execroot}" ;;\n'
        '  *" cquery "*) echo "goimports goimports" ;;\n'
        "esac",
    )
    for name in ("goimports", "clang-format", "buildifier"):
      self.tool(self.formatter(name), "exit 0")

  def tool(self, path: pathlib.Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)

  def formatter(self, name: str) -> pathlib.Path:
    if name == "goimports":
      return self.execroot / "goimports"
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

  def test_clang_tidy_process_failure(self) -> None:
    tool = self.bin / "clang-tidy"
    self.tool(tool, "echo driver-stdout; echo driver-stderr >&2; exit 7")
    source = self.root / "source.cc"
    source.touch()
    database = self.root / "compile_commands.json"
    database.write_text(json.dumps([
        {"directory": str(self.root), "file": str(source)},
    ]))
    result = subprocess.run(
        [
            sys.executable,
            clang_tidy.__file__,
            f"--clang-tidy={tool}",
            f"--config-file={self.root / '.clang-tidy'}",
            f"--database={database}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
    self.assertIn("clang-tidy exited with status 7", result.stderr)
    self.assertIn("driver-stdout", result.stderr)
    self.assertIn("driver-stderr", result.stderr)

  def test_bazel_failure(self) -> None:
    self.tool(self.bin / "bazel", "echo bazel-build-failed >&2; exit 7")
    result = self.lint("goimports")
    self.assert_failed(result, "goimports")
    self.assertIn("bazel-build-failed", result.stderr)

  def test_diagnostics_and_continuation(self) -> None:
    self.tool(
        self.formatter("clang-format"), "echo formatter-crashed >&2; exit 7"
    )
    result = self.lint("clang-format", "buildifier")
    self.assert_failed(result, "clang-format")
    self.assertIn("formatter-crashed", result.stderr)
    self.assertIn("PASS  buildifier", result.stderr)

  def test_installer_failure(self) -> None:
    # Both failures occur in an installer command substitution. A stale cache
    # entry must not be made executable by commands following the failure.
    self.formatter("buildifier").chmod(0o644)
    result = self.lint("buildifier")
    self.assert_failed(result, "buildifier")
    self.assertIn("unexpected-download", result.stderr)
    self.assertEqual(self.formatter("buildifier").stat().st_mode & 0o111, 0)
    # mktemp adds another substitution, whose status local must not mask.
    self.tool(self.bin / "mktemp", "echo temporary-file-failed >&2; exit 7")
    result = self.lint("buildifier", "goimports")
    self.assert_failed(result, "buildifier")
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


class CompileCommandsTest(unittest.TestCase):

  def test_database_publication(self) -> None:
    with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as temp:
      workspace = pathlib.Path(temp)
      tools = workspace / "tools"
      tools.mkdir()
      generator = tools / "gen_compile_commands.sh"
      shutil.copyfile(LINT.with_name("gen_compile_commands.sh"), generator)
      bazel = workspace / "selected bazel"
      bazel.write_text("""#!/bin/sh
case "$1" in
  run)
    for arg; do
      case "$arg" in --output=*) printf generated > "${arg#--output=}";; esac
    done
    if [ "$FAIL_STEP" = run ]; then echo generation-failed >&2; exit 7; fi
    ;;
  build)
    # Preparation must see the previous database and the newly generated file.
    test "$(cat compile_commands.json)" = previous || exit 8
    set -- compile_commands.json.*
    test -f "$1" && test "$(cat "$1")" = generated || exit 8
    if [ "$FAIL_STEP" = build ]; then echo preparation-failed >&2; exit 7; fi
    ;;
  *) exit 8;;
esac
""")
      bazel.chmod(0o755)
      database = workspace / "compile_commands.json"
      for fail_step in ("run", "build", ""):
        with self.subTest(fail_step=fail_step):
          database.write_text("previous")
          result = subprocess.run(
              ["bash", str(generator), str(bazel)],
              env=dict(os.environ, FAIL_STEP=fail_step),
              capture_output=True,
              text=True,
              check=False,
          )
          self.assertEqual(
              result.returncode, 7 if fail_step else 0,
              result.stdout + result.stderr,
          )
          self.assertEqual(
              database.read_text(), "previous" if fail_step else "generated"
          )
          self.assertEqual(list(workspace.glob("compile_commands.json.*")), [])


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
