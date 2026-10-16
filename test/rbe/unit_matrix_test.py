#!/usr/bin/env python3
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

"""Selection coverage using retained graph-declared syscall partitions."""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import unit_matrix


FIXTURE = Path(__file__).parent / "testdata/syscall_buckets"


class SyscallBucketsTest(unittest.TestCase):
    def select(self, value: str | None, routing: Path = FIXTURE / "routing.jsonl"):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as stdout:
            output = Path(directory) / "targets"
            unit_matrix.select_profile(
                str(FIXTURE / "profile.jsonl"), "arm64",
                str(routing), str(FIXTURE / "actions.json"), str(output),
                syscall_policy=True, hybrid=True,
                selected_buckets=unit_matrix.syscall_buckets(value) if value is not None else None,
            )
            selection = json.loads(stdout.getvalue())
            self.assertEqual(output.read_text().splitlines(), selection["selected_owners"])
            return selection

    def test_single_and_union_partition(self):
        expected = json.loads((FIXTURE / "expected.json").read_text())
        full = self.select(None)
        selected = set()
        for bucket in range(15):
            with self.subTest(bucket=bucket):
                single = self.select(str(bucket))
                self.assertEqual(single["selected_owners"], expected[str(bucket)])
                self.assertEqual(single["syscall_bucket"], bucket)
                self.assertEqual(single["selected_syscall_buckets"], [bucket])
                self.assertEqual(single["syscall_buckets"], expected)
                self.assertFalse(selected.intersection(single["selected_owners"]), "Buckets must be disjoint")
                selected.update(single["selected_owners"])
        self.assertEqual(sorted(selected), full["selected_owners"])
        self.assertEqual(full["unexecuted_bucket_owners"], [])
        self.assertEqual(self.select(",".join(map(str, range(15))))["selected_owners"], full["selected_owners"])

    def test_remaining_buckets_exclude_zero(self):
        expected = json.loads((FIXTURE / "expected.json").read_text())
        remaining = self.select(",".join(map(str, range(1, 15))))
        self.assertEqual(remaining["selected_owners"], sorted(label for bucket in range(1, 15) for label in expected[str(bucket)]))
        self.assertEqual(remaining["unexecuted_bucket_owners"], expected["0"])
        self.assertIsNone(remaining["syscall_bucket"])
        self.assertEqual(remaining["selected_syscall_buckets"], list(range(1, 15)))
        bounded = self.select("1,4,14")
        self.assertEqual(bounded["selected_owners"], sorted(label for bucket in (1, 4, 14) for label in expected[str(bucket)]))
        self.assertEqual(sorted(bounded["selected_owners"] + bounded["unexecuted_bucket_owners"]), self.select(None)["selected_owners"])

    def test_invalid_graph_partition(self):
        original = (FIXTURE / "routing.jsonl").read_text()
        for tags in ([], ["hash15:0", "hash15:1"], ["hash15:15"]):
            with self.subTest(tags=tags), tempfile.TemporaryDirectory() as directory:
                events = [json.loads(line) for line in original.splitlines()]
                configured = next(event["configured"] for event in events if "configured" in event)
                configured["tag"] = [tag for tag in configured["tag"] if not tag.startswith("hash15:")] + tags
                routing = Path(directory) / "routing.jsonl"
                routing.write_text("".join(json.dumps(event) + "\n" for event in events))
                with self.assertRaisesRegex(ValueError, "one graph-declared hash15 bucket"):
                    self.select("1,2", routing)

    def test_empty_requested_bucket(self):
        with tempfile.TemporaryDirectory() as directory:
            events = [json.loads(line) for line in (FIXTURE / "routing.jsonl").read_text().splitlines()]
            for event in events:
                if "configured" in event:
                    event["configured"]["tag"] = ["hash15:0" if tag == "hash15:1" else tag for tag in event["configured"]["tag"]]
            routing = Path(directory) / "routing.jsonl"
            routing.write_text("".join(json.dumps(event) + "\n" for event in events))
            with self.assertRaisesRegex(ValueError, "no owners in syscall bucket 1"):
                self.select("0,1", routing)

    def test_invalid_bucket_sets(self):
        for value in ("", "15", "-1", "01", "1,", ",1", "1,,2", "1,1", "2,1", "0,15", "1-14", "1 2", "1, 2", "1\n2"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                unit_matrix.syscall_buckets(value)

    def test_workflow_input_rejected_before_bazel(self):
        root = Path(__file__).parents[2]
        environment = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
        for value in ("15", "1,1", "2,1", "1,,2"):
            with self.subTest(value=value):
                result = subprocess.run(
                    ["bash", "test/rbe/qualify.sh", "--arch=arm64", f"--syscall-bucket={value}", "syscalls-64k"],
                    cwd=root, env=environment, capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("syscall-buckets", result.stderr)
        result = subprocess.run(
            ["bash", "test/rbe/qualify.sh", "--arch=arm64", "--syscall-bucket=1,2", "unit"],
            cwd=root, env=environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("supported local or guest syscall profile", result.stderr)


if __name__ == "__main__":
    unittest.main()
