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

"""Tests for credential scope and fail-closed behavior."""

import io
import json
import unittest
from pathlib import Path
import tempfile
from unittest import mock

import credential_helper


class CredentialHelperTest(unittest.TestCase):
    def invoke(self, uri: str, key: str | None = "test-only-value") -> str:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "key"
            if key is not None:
                key_file.write_text(key)
            with (
                mock.patch("sys.argv", ["credential_helper", "get"]),
                mock.patch("sys.stdin", io.StringIO(json.dumps({"uri": uri}))),
                mock.patch("sys.stdout", output),
                mock.patch.dict("os.environ", {"BUILDBUDDY_API_KEY_FILE": str(key_file)}, clear=True),
            ):
                credential_helper.main()
        return output.getvalue()

    def test_tls_endpoint(self) -> None:
        for scheme in ("https", "grpcs"):
            with self.subTest(scheme=scheme):
                self.assertEqual(
                    json.loads(self.invoke(f"{scheme}://remote.buildbuddy.io:443/path")),
                    {"headers": {"x-buildbuddy-api-key": ["test-only-value"]}},
                )

    def test_reject_untrusted_authority(self) -> None:
        for uri in (
            "http://remote.buildbuddy.io", "grpc://remote.buildbuddy.io",
            "https://remote.buildbuddy.io.evil.example", "https://evil.example",
            "https://user@remote.buildbuddy.io", "https://remote.buildbuddy.io:80",
        ):
            with self.subTest(uri=uri), self.assertRaises(SystemExit):
                self.invoke(uri)

    def test_missing_credential_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.invoke("https://remote.buildbuddy.io", None)

    def test_reject_unusable_key(self) -> None:
        for key in ("", "bad\r\nheader", "nonascii-\N{SNOWMAN}"):
            with self.subTest(key_present=key is not None), self.assertRaises(SystemExit):
                self.invoke("https://remote.buildbuddy.io", key)


if __name__ == "__main__":
    unittest.main()
