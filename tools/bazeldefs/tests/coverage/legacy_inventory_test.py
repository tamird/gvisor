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

"""Check the coverage migration against its retained source-bound inventory."""

import gzip
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import unittest

from python.runfiles import runfiles


class LegacyInventoryTest(unittest.TestCase):
    sentry: Path
    baseline: Path

    def test_positions(self) -> None:
        compressed = self.baseline.read_bytes()
        self.assertEqual(
            hashlib.sha256(compressed).hexdigest(),
            "b6e0dca3554ff808626ec62dd36b22978c6cee6036108422c9be5eb8352f55be",
        )
        raw = gzip.decompress(compressed)
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "c6e30fc3640a93367ca0d6ea7bfe05dd8dc6c5cd5961103c32809c890d1ce576",
        )
        legacy = set(raw.decode().splitlines())
        self.assertEqual(len(legacy), 115841)
        protobuf = {position for position in legacy if ".pb.go:" in position}
        self.assertEqual(len(protobuf), 2418)

        result = subprocess.run(
            [self.sentry, "symbolize", "-all"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = iter(result.stdout.splitlines())
        current: set[str] = set()
        pcs: set[str] = set()
        for pc in lines:
            self.assertRegex(pc, r"^0x[0-9a-f]+$")
            self.assertNotIn(pc, pcs)
            pcs.add(pc)
            position = next(lines, None)
            self.assertIsNotNone(position, f"missing position after {pc}")
            assert position is not None
            match = re.fullmatch(r".+:([0-9]+)\.([0-9]+),([0-9]+)\.([0-9]+)", position)
            self.assertIsNotNone(match, f"invalid position: {position}")
            assert match is not None
            start_line, start_column, end_line, end_column = map(int, match.groups())
            self.assertGreater(min(start_line, start_column, end_line, end_column), 0)
            self.assertGreaterEqual((end_line, end_column), (start_line, start_column))
            current.add(position)

        self.assertTrue(any("/pkg/sentry/kernel/" in position for position in current))
        forbidden = re.compile(r"/pkg/(coverage/|ring0/|sleep/|sync/|syncevent/|bpf/|sentry/platform/)")
        self.assertEqual(sorted(position for position in current if forbidden.search(position)), [])
        missing = sorted(legacy - current)
        self.assertEqual(len(missing), 0, f"missing {len(missing)} positions; first 20: {missing[:20]}")
        print(
            f"legacy={len(legacy)} candidate={len(current)} "
            f"protobuf={len(protobuf & current)} additional={len(current - legacy)}"
        )


if __name__ == "__main__":
    match sys.argv:
        case [program, sentry, baseline, *test_args]:
            resolver = runfiles.Create()
            if resolver is None:
                raise RuntimeError("Bazel runfiles are unavailable")
            sentry_path = resolver.Rlocation(sentry)
            baseline_path = resolver.Rlocation(baseline)
            if sentry_path is None or baseline_path is None:
                raise FileNotFoundError(f"missing declared runfiles: {sentry}, {baseline}")
            LegacyInventoryTest.sentry = Path(sentry_path)
            LegacyInventoryTest.baseline = Path(baseline_path)
            unittest.main(argv=[program, *test_args])
        case _:
            raise ValueError("expected Sentry and baseline runfile paths")
