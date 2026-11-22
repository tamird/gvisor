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

"""Check the declared Sentry's complete static coverage inventory."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

from python.runfiles import runfiles


class InventoryTest(unittest.TestCase):
    sentry: Path
    legacy_sentry: Path | None = None
    source_archive: Path | None = None

    def _positions(self, sentry: Path) -> set[str]:
        result = subprocess.run(
            [sentry, "symbolize", "-all"],
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
        protobuf = {position for position in current if ".pb.go:" in position}
        self.assertGreater(len(protobuf), 0, "no generated protobuf coverage positions")
        forbidden = re.compile(r"/pkg/(coverage/|ring0/|sleep/|sync/|syncevent/|bpf/|sentry/platform/)")
        self.assertEqual(sorted(position for position in current if forbidden.search(position)), [])
        return current

    def test_positions(self) -> None:
        current = self._positions(self.sentry)
        result: dict[str, object] = {
            "positions": len(current),
            "protobuf_positions": sum(".pb.go:" in position for position in current),
        }
        if self.legacy_sentry is not None:
            legacy = self._positions(self.legacy_sentry)
            missing = legacy - current
            self.assertFalse(missing, f"{len(missing)} missing positions: {sorted(missing)[:20]}")
            result.update(
                legacy_positions=len(legacy),
                legacy_protobuf_positions=sum(".pb.go:" in position for position in legacy),
                additional_positions=len(current - legacy),
                missing_positions=0,
            )
        for name, binary in (("candidate_binary", self.sentry),
                             ("legacy_binary", self.legacy_sentry),
                             ("source_export", self.source_archive)):
            if binary is not None:
                with binary.open("rb") as source:
                    result[name + "_sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sentry")
    parser.add_argument("--legacy-sentry")
    parser.add_argument("--source-archive")
    args, test_args = parser.parse_known_args()
    resolver = runfiles.Create()
    if resolver is None:
        raise RuntimeError("Bazel runfiles are unavailable")

    def resolve(name: str) -> Path:
        path = resolver.Rlocation(name)
        if path is None:
            raise FileNotFoundError(f"missing declared runfile: {name}")
        return Path(path)

    InventoryTest.sentry = resolve(args.sentry)
    if args.legacy_sentry is not None:
        InventoryTest.legacy_sentry = resolve(args.legacy_sentry)
    if args.source_archive is not None:
        InventoryTest.source_archive = resolve(args.source_archive)
    unittest.main(argv=[sys.argv[0], *test_args])
