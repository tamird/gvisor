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

"""Synthetic credential and artifact publication boundaries."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CredentialArtifactsTest(unittest.TestCase):

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.private = root / "private"
        self.private.mkdir(mode=0o700)
        self.public = root / "artifacts"
        self.public.mkdir()
        self.output = root / "github-output"
        self.sentinel = 'SYNTHETIC-CREDENTIAL-"\\-DO-NOT-PUBLISH'
        self.script = Path(__file__).with_name("credential_artifacts.py")
        self.env = {**os.environ, "BUILDBUDDY_API_KEY": self.sentinel,
                    "GITHUB_OUTPUT": str(self.output)}
        prepared = self.call("prepare", self.private)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.env.pop("BUILDBUDDY_API_KEY")

    def call(self, *args):
        return subprocess.run([sys.executable, str(self.script), *map(str, args)],
                              env=self.env, capture_output=True, check=False)

    def test_native_helper_protocol_and_failure(self):
        helper = self.private / "helper"
        result = subprocess.run([helper, "get"], input=b'{"uri":"https://remote.buildbuddy.io"}',
                                env=self.env, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(json.loads(result.stdout),
                         {"headers": {"x-buildbuddy-api-key": [self.sentinel]}})
        self.assertNotIn(self.sentinel.encode(), helper.read_bytes())
        (self.private / "helper.json").unlink()
        failed = subprocess.run([helper, "get"], input=b'{}', env=self.env,
                                capture_output=True, check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stdout, b"")
        self.assertEqual(failed.stderr, b"")

    def test_success_and_failure_evidence_can_publish(self):
        for native_exit in (0, 7):
            with self.subTest(native_exit=native_exit):
                self.output.unlink(missing_ok=True)
                (self.public / "native-exit").write_text(str(native_exit) + "\n")
                result = self.call("verify", self.private, self.public)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(self.output.read_text(), "artifacts_safe=true\n")
                self.assertEqual((self.public / "native-exit").read_text(), str(native_exit) + "\n")

    def test_credentials_withhold_success_and_failure_evidence(self):
        for native_exit in (0, 7):
            for value in (self.sentinel.encode(), json.dumps(self.sentinel)[1:-1].encode()):
                with self.subTest(native_exit=native_exit, escaped=value != self.sentinel.encode()):
                    self.output.unlink(missing_ok=True)
                    (self.public / "native-exit").write_text(str(native_exit) + "\n")
                    # Cross the scanner's block boundary, as a large failure log can.
                    (self.public / "log").write_bytes(b"x" * (1024 * 1024 - 8) + value)
                    result = self.call("verify", self.private, self.public)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(self.output.exists())
                    self.assertNotIn(value, result.stdout + result.stderr)

    def test_private_file_and_filename_cannot_publish(self):
        (self.public / "credentials").symlink_to(self.private / "helper.json")
        self.assertNotEqual(self.call("verify", self.private, self.public).returncode, 0)
        self.assertFalse(self.output.exists())
        (self.public / "credentials").unlink()
        (self.public / self.sentinel).write_text("innocuous contents")
        self.assertNotEqual(self.call("verify", self.private, self.public).returncode, 0)
        self.assertFalse(self.output.exists())

    def test_unreadable_directory_cannot_publish(self):
        self.assertNotEqual(os.geteuid(), 0, "Run this boundary test as a nonroot user")
        child = self.public / "unreadable"
        child.mkdir()
        (child / "secret").write_text(self.sentinel)
        child.chmod(0)
        self.addCleanup(child.chmod, 0o700)
        result = self.call("verify", self.private, self.public)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertNotIn(self.sentinel.encode(), result.stdout + result.stderr)

    def test_missing_inventory_cannot_publish(self):
        (self.private / "helper.json").unlink()
        result = self.call("verify", self.private, self.public)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertNotIn(self.sentinel.encode(), result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
