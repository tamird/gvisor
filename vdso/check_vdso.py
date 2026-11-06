# Copyright 2018 The gVisor Authors.
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

"""Run the vDSO checker for distribution builds that invoke this file."""

import os
from pathlib import Path
import sys


if __name__ == "__main__":
  checker = Path(__file__).resolve().parent / "check" / "main.go"
  # Only the standard library is needed, even in a gVisor source checkout
  # whose go.mod requests a newer toolchain than the distribution ships.
  os.environ["GO111MODULE"] = "off"
  # The checker runs on the build host when the input ELF is cross-compiled.
  os.environ.pop("GOARCH", None)
  os.environ.pop("GOOS", None)
  os.execvp("go", ["go", "run", str(checker), *sys.argv[1:]])
