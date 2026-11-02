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

"""Run codespell on the declared sources without allowing in-place edits."""

import configparser
import os
from pathlib import Path
import sys


def main() -> None:
  entry, config, root, output = (Path(arg).absolute() for arg in sys.argv[1:5])
  options = configparser.ConfigParser(interpolation=None)
  with config.open() as f:
    options.read_file(f)
  # Codespell accepts abbreviations in the config as well as on the command line.
  if any("write-changes".startswith(key) for key in options["codespell"]):
    sys.exit("the spelling check cannot enable write-changes")

  sys.path.insert(0, str(entry.parent.parent))
  from codespell_lib import main as check_spelling

  os.chdir(root)
  status = check_spelling("--config", str(config), "--", *sys.argv[5:])
  if status:
    sys.exit(status)
  output.touch()


if __name__ == "__main__":
  main()
