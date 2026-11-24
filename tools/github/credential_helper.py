#!/usr/bin/env python3
# Copyright 2026 The gVisor Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Bazel credentials for this fork's qualification artifact downloads."""

import json
import os
import re
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
    if (
        endpoint.scheme != "https"
        or endpoint.netloc != "api.github.com"
        or endpoint.query
        or endpoint.fragment
        or not re.fullmatch(
            r"/repos/tamird/gvisor/actions/artifacts/[0-9]+/zip", endpoint.path
        )
    ):
        raise SystemExit("Credential URI is outside qualification artifacts")
    if not (token := os.environ.get("GITHUB_TOKEN")):
        raise SystemExit("GITHUB_TOKEN is required")
    if any(ord(character) < 32 or ord(character) > 126 for character in token):
        raise SystemExit("GitHub credential must contain printable ASCII")
    json.dump({"headers": {"Authorization": [f"Bearer {token}"]}}, sys.stdout)


if __name__ == "__main__":
    main()
