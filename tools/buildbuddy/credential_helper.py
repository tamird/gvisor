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

"""Bazel credential helper for the BuildBuddy remote service."""

import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit


def main() -> None:
    if sys.argv[1:] != ["get"]:
        raise SystemExit("Expected credential-helper get request")
    match json.load(sys.stdin):
        case {"uri": str(uri)}:
            endpoint = urlsplit(uri)
        case _:
            raise SystemExit("Expected credential-helper URI")
    if endpoint.scheme not in ("https", "grpcs") or endpoint.netloc not in (
        "remote.buildbuddy.io", "remote.buildbuddy.io:443"
    ):
        raise SystemExit("Credential URI is outside the BuildBuddy TLS endpoint")
    if not (key_file := os.environ.get("BUILDBUDDY_API_KEY_FILE")):
        raise SystemExit("BUILDBUDDY_API_KEY_FILE is required")
    key = Path(key_file).read_text()
    if not key:
        raise SystemExit("BuildBuddy credential file is empty")
    if any(ord(character) < 32 or ord(character) > 126 for character in key):
        raise SystemExit("BuildBuddy credential must contain printable ASCII")
    json.dump({"headers": {"x-buildbuddy-api-key": [key]}}, sys.stdout)


if __name__ == "__main__":
    main()
