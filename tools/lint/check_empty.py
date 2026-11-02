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

"""Print a tool's report and fail if it contains diagnostics."""

from pathlib import Path
import sys


if __name__ == "__main__":
    report = Path(sys.argv[1]).read_bytes()
    sys.stderr.buffer.write(report)
    sys.exit(bool(report))
