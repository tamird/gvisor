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

"""Generates compile_commands.json from Bazel aquery output."""

import argparse
import json
import os
import pathlib
import shlex
import subprocess
from typing import cast


def _bazel(bazel: list[str], options: list[str], command: str, *args: str) -> str:
  """Runs a bazel command with the given options, returning its stdout."""

  return subprocess.run(
      bazel + [command] + options + list(args),
      check=True,
      text=True,
      stdout=subprocess.PIPE,
  ).stdout


def _compile_arguments(
    bazel: list[str], options: list[str], targets: list[str]
) -> list[list[str]]:
  """Returns the compiler arguments for the targets' C++ dependencies."""

  query = 'mnemonic("CppCompile", deps({}))'.format(" union ".join(targets))
  decoded: object = json.loads(
      _bazel(
          bazel,
          options,
          "aquery",
          "--output=jsonproto",
          "--noshow_progress",
          query,
      )
  )
  if not isinstance(decoded, dict):
    raise ValueError("Bazel aquery output must be an object")
  records = cast(dict[object, object], decoded).get("actions")
  if not isinstance(records, list):
    raise ValueError("Bazel aquery actions must be an array")
  commands: list[list[str]] = []
  for record in cast(list[object], records):
    if not isinstance(record, dict):
      raise ValueError("Bazel aquery action must be an object")
    value = cast(dict[object, object], record).get("arguments")
    if not isinstance(value, list):
      raise ValueError("Bazel compile action arguments must be an array of strings")
    arguments: list[str] = []
    for arg in cast(list[object], value):
      if not isinstance(arg, str):
        raise ValueError("Bazel compile action arguments must be an array of strings")
      arguments.append(arg)
    commands.append(arguments)
  return commands


class _Args(argparse.Namespace):
  bazel: str = "bazel"
  bazel_options: str = ""
  targets: list[str] = []


def main() -> int:
  parser = argparse.ArgumentParser(
      description="Generates compile_commands.json for gVisor's C++ code."
  )
  _ = parser.add_argument(
      "--bazel",
      default="bazel",
      help=(
          "Bazel command, including any startup options (default: %(default)s)."
      ),
  )
  _ = parser.add_argument(
      "--bazel-options",
      default="",
      help="Options passed through to Bazel calls.",
  )
  _ = parser.add_argument(
      "targets",
      nargs="*",
      default=["//test/...", "//tools/..."],
      help="Bazel targets to index (default: %(default)s).",
  )
  args = parser.parse_args(namespace=_Args())
  targets = args.targets
  bazel = shlex.split(args.bazel)
  options = shlex.split(args.bazel_options)

  workspace = pathlib.Path(__file__).parent.parent.resolve()
  os.chdir(workspace)

  _ = _bazel(
      bazel,
      options,
      "build",
      "--aspects=//tools:cc_compile_inputs.bzl%cc_compile_inputs",
      "--output_groups=cc_compile_inputs",
      *targets,
  )
  execroot = pathlib.Path(
      _bazel(bazel, options, "info", "execution_root").strip()
  )

  database: list[dict[str, str | list[str]]] = []
  seen: set[pathlib.Path] = set()
  for arguments in _compile_arguments(bazel, options, targets):
    if "-c" not in arguments:
      continue
    source = pathlib.Path(arguments[arguments.index("-c") + 1])

    # Skip third-party code.
    if "external" in source.parts:
      continue

    # A source can be compiled by several actions.
    # Skip all but the first.
    if source in seen:
      continue
    seen.add(source)

    # Add the compile command to the database.
    database.append({
        # Special-case for generated files in bazel-out/. When LSP jump is used,
        # it will navigate to the execroot path, so match that in the database.
        "file": str(
            (execroot if source.parts[0] == "bazel-out" else workspace) / source
        ),
        "directory": str(execroot),
        "arguments": arguments,
    })

  path = os.path.join(workspace, "compile_commands.json")
  with open(path, "w") as out:
    json.dump(database, out, indent=4)
  print(f"generated {path}: {len(database)} entries")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
