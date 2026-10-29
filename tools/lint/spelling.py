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

"""Run Codespell on the projected inputs, keeping its normal diagnostics and exit."""

import configparser
import json
import os
from pathlib import Path
import runpy
import sys

_CONFIG = "tools/.codespellrc"


def main() -> None:
  module, manifest, output = (Path(arg).absolute() for arg in sys.argv[1:])
  inputs = json.loads(manifest.read_text())
  if inputs["data"] != [_CONFIG] or not inputs["sources"]:
    sys.exit("codespell requires tools/.codespellrc and its declared sources")

  # The wheel provides its dictionaries under the same import root as __main__.
  sys.path.insert(0, str(module.parent.parent))
  os.chdir(manifest.parent / "src")
  config = configparser.ConfigParser(interpolation=None)
  with Path(_CONFIG).open() as f:
    config.read_file(f)
  # Codespell's parser also accepts unambiguous prefixes of its long options.
  if config.has_section("codespell") and any(
      "write-changes".startswith(option) for option in config["codespell"]
  ):
    sys.exit("the source spelling check cannot enable write-changes")
  sys.argv = ["codespell", "--config", _CONFIG, "--", *inputs["sources"]]
  try:
    runpy.run_module("codespell_lib", run_name="__main__", alter_sys=True)
  except SystemExit as e:
    if e.code:
      raise
  output.touch()


if __name__ == "__main__":
  main()
