"""Exercise bounded bootstrap diagnostics and native-status preservation."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import BinaryIO
from unittest import mock

import image_bootstrap


class DiagnosticExcerptTest(unittest.TestCase):
    def test_complete_redacted_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary) / "log"
            original = b"x-buildbuddy-api-key=secret-value\nfinal partial line"
            raw.write_bytes(original)
            data, record = image_bootstrap.diagnostic_excerpt(raw, b"secret-value")
            self.assertEqual(data, original.replace(b"secret-value", b"[REDACTED]"))
            self.assertEqual(record["retained_ranges"], [[0, len(original)]])
            self.assertEqual(record["omitted_bytes"], 0)
            self.assertFalse(record["truncated"])
            self.assertEqual(record["sha256"], hashlib.sha256(data).hexdigest())

    def test_boundary_credentials_and_exact_omissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary) / "log"
            secret = b"private-token"
            # Both 32-byte selection boundaries cut through secret-bearing
            # lines. Neither clipped fragment may survive in the excerpt.
            head = b"first\n"
            tail = b"last\n"
            original = head + b"x" * 20 + secret + b"\n" + b"middle\n" * 20 + secret + b"y" * 20 + b"\n" + tail
            raw.write_bytes(original)
            data, record = image_bootstrap.diagnostic_excerpt(raw, secret, limit=64)
            self.assertEqual(record["retained_ranges"], [[0, len(head)], [len(original) - len(tail), len(original)]])
            self.assertEqual(record["retained_raw_bytes"], len(head) + len(tail))
            self.assertEqual(record["omitted_bytes"], len(original) - len(head) - len(tail))
            self.assertEqual(record["raw_bytes"], len(original))
            self.assertTrue(data.startswith(head))
            self.assertTrue(data.endswith(tail))
            self.assertNotIn(b"private", data)
            self.assertNotIn(b"token", data)

    def test_aligned_tail_and_unterminated_giant_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary) / "log"
            raw.write_bytes(b"one\n" + b"x" * 20 + b"\ntail\nend")
            data, record = image_bootstrap.diagnostic_excerpt(raw, b"secret", limit=16)
            self.assertTrue(data.endswith(b"tail\nend"))
            self.assertEqual(record["retained_ranges"], [[0, 4], [25, 33]])
            raw.write_bytes(b"x" * 100)
            data, record = image_bootstrap.diagnostic_excerpt(raw, b"secret", limit=16)
            self.assertEqual(record["retained_ranges"], [])
            self.assertEqual(record["omitted_bytes"], 100)
            self.assertNotIn(b"xxx", data)

    def test_unknown_header_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary) / "log"
            raw.write_bytes(b"x-buildbuddy-api-key=unrecognized\n")
            with self.assertRaisesRegex(ValueError, "Unredacted credential"):
                image_bootstrap.diagnostic_excerpt(raw, b"known-secret")

    def test_large_native_stream_preserves_exit(self) -> None:
        for native_exit in (0, 7):
            with self.subTest(native_exit=native_exit), tempfile.TemporaryDirectory() as temporary:
                artifacts = Path(temporary)
                calls = 0

                def run(command: list[str], *, stdout: BinaryIO, stderr: BinaryIO) -> subprocess.CompletedProcess[bytes]:
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        launch = next(arg.removeprefix("--script_path=") for arg in command if arg.startswith("--script_path="))
                        Path(launch).write_text("#!/bin/sh\n")
                        return subprocess.CompletedProcess(command, 0)
                    stdout.write(b"head\n" + b"x\n" * (image_bootstrap.DIAGNOSTIC_LIMIT // 2) + b"tail\n")
                    stderr.write(b"x-buildbuddy-api-key=known-secret\n")
                    return subprocess.CompletedProcess(command, native_exit)

                environment = {
                    "BUILDBUDDY_API_KEY": "known-secret",
                    "APPLICABILITY_QUALIFICATION_CONFIG": json.dumps({"source": "exact-source"}),
                    "BUILDBUDDY_ARTIFACTS_DIRECTORY": str(artifacts),
                }
                stdout = mock.Mock(buffer=io.BytesIO())
                stderr = mock.Mock(buffer=io.BytesIO())
                with mock.patch.dict(os.environ, environment), mock.patch.object(image_bootstrap.subprocess, "check_output", side_effect=["exact-source\n", b"", "/usr/bin/bazel\n"]), mock.patch.object(image_bootstrap.subprocess, "run", side_effect=run), mock.patch.object(image_bootstrap.sys, "stdout", stdout), mock.patch.object(image_bootstrap.sys, "stderr", stderr):
                    self.assertEqual(image_bootstrap.main(), native_exit)
                record = json.loads((artifacts / "bootstrap.json").read_text())
                self.assertEqual(record["native_exit"], native_exit)
                self.assertTrue(record["outputs"]["stdout"]["truncated"])
                self.assertGreater(record["outputs"]["stdout"]["omitted_bytes"], 0)
                self.assertLessEqual(record["outputs"]["stdout"]["retained_raw_bytes"], image_bootstrap.DIAGNOSTIC_LIMIT)
                self.assertEqual((artifacts / "bootstrap-stderr.log").read_bytes(), b"x-buildbuddy-api-key=[REDACTED]\n")


if __name__ == "__main__":
    unittest.main()
