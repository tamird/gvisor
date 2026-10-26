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

import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from credential_artifacts import verify


class CredentialArtifactsTest(unittest.TestCase):

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.private = root / "private"
        self.private.mkdir(mode=0o700)
        self.public = root / "artifacts"
        self.public.mkdir()
        self.bundle = root / "qualification.tar.gz"
        self.verdict = self.public / "verdict.json"
        self.verdict.write_text('{"native_exit":0}\n')
        self.sentinel = 'SYNTHETIC-CREDENTIAL-"\\-DO-NOT-PUBLISH'
        self.script = Path(__file__).with_name("credential_artifacts.py")
        self.env = {**os.environ, "BUILDBUDDY_API_KEY": self.sentinel}
        prepared = self.call("prepare", self.private)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.env.pop("BUILDBUDDY_API_KEY")

    def call(self, *args):
        return subprocess.run([sys.executable, str(self.script), *map(str, args)],
                              env=self.env, capture_output=True, check=False)

    def archive(self, **members) -> None:
        with tarfile.open(self.bundle, "w:gz") as archive:
            for name, data in {"verdict.json": self.verdict.read_bytes(), **members}.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))

    def verify(self):
        return self.call("verify", self.private, self.bundle, self.verdict)

    def assert_withheld(self) -> None:
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(self.sentinel.encode(), result.stdout + result.stderr)

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
                self.verdict.write_text(json.dumps({"native_exit": native_exit}))
                self.archive(**{"invocation-example/profile.json.gz": gzip.compress(b"[]")})
                result = self.verify()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(self.verdict.read_bytes()), {"native_exit": native_exit})

    def test_credentials_withhold_success_and_failure_evidence(self):
        for value in (self.sentinel.encode(), json.dumps(self.sentinel)[1:-1].encode()):
            # Cross the scanner's block boundary inside a compressed member.
            profile = gzip.compress(b"x" * (1024 * 1024 - 8) + value)
            self.assertNotIn(value, profile)
            self.archive(**{"invocation-example/profile.json.gz": profile})
            self.assert_withheld()

    def test_compact_log_uses_archived_zstandard_payload(self):
        value = self.sentinel.encode()
        prefix = (value[:16] + b" record " + value[16:] + b"\n") * 100
        for leaked in (False, True):
            with self.subTest(leaked=leaked):
                payload = prefix + (value if leaked else b"safe")
                log = subprocess.run([self.env["QUALIFICATION_ZSTD"], "-qc"], input=payload,
                                     capture_output=True, check=True).stdout
                self.assertNotIn(value, log)
                self.archive(**{"invocation-example/execution.binpb": log})
                if leaked:
                    self.assert_withheld()
                else:
                    verify(self.private, self.bundle, self.verdict)

    def test_loose_verdict_must_match_the_archive(self):
        self.archive()
        for replacement in (self.sentinel, '{"native_exit":1}'):
            self.verdict.write_text(replacement)
            self.assert_withheld()

    def test_unsupported_members_cannot_publish(self):
        for name, data in ((self.sentinel, b"harmless contents"),
                           ("outputs.zip", gzip.compress(b"not inspected")),
                           ("test.log", gzip.compress(self.sentinel.encode())),
                           ("profile.json.gz", b"truncated profile")):
            with self.subTest(name=name):
                self.archive(**{name: data})
                self.assert_withheld()

    def test_unreadable_or_partial_upload_cannot_publish(self):
        self.archive()
        self.bundle.chmod(0)
        try:
            self.assert_withheld()
        finally:
            self.bundle.chmod(0o600)
        self.bundle.write_bytes(self.bundle.read_bytes()[:-8])
        self.assert_withheld()

    def test_missing_inventory_cannot_publish(self):
        self.archive()
        (self.private / "helper.json").unlink()
        self.assert_withheld()


if __name__ == "__main__":
    unittest.main()
