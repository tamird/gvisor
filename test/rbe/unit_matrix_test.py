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
    def select(
        self, value: str | None, routing: Path = FIXTURE / "routing.jsonl",
        *, owners: list[str] | None = None, rc_arch: str | None = None,
        mixed_rc_owners: bool = False, actions: Path = FIXTURE / "actions.json",
    ):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as stdout:
            output = Path(directory) / "targets"
            if rc_arch is not None:
                # Project the retained owners into the two declared RC routing
                # contracts; the real one-owner run checks graph integration.
                suffix = "_rc_kvm" if rc_arch == "amd64" else "_rc_tcg"
                events = [json.loads(line.replace("_arm64", suffix)) for line in routing.read_text().splitlines()]
                for event in events:
                    if "configured" in event:
                        event["configured"]["tag"] += (
                            ["amd64-rc-kvm", "no-remote-exec", "no-sandbox"]
                            if rc_arch == "amd64" else ["arm64-rc-tcg", "no-local"]
                        )
                routing = Path(directory) / "routing.jsonl"
                routing.write_text("".join(json.dumps(event) + "\n" for event in events))
                graph = json.loads(actions.read_text().replace("_arm64", suffix))
                for action in graph["actions"]:
                    properties = {item["key"]: item.get("value", "") for item in action["executionInfo"]}
                    properties.update({"Arch": "amd64", "workload-isolation-type": "oci", "dockerUser": "nobody"})
                    properties.pop("no-remote-exec", None)
                    properties.update({"no-remote-exec": "", "no-sandbox": ""} if rc_arch == "amd64" else {"no-local": ""})
                    action["executionInfo"] = [{"key": key, "value": value} for key, value in properties.items()]
                actions = Path(directory) / "actions.json"
                actions.write_text(json.dumps(graph))
            unit_matrix.select_profile(
                str(FIXTURE / "profile.jsonl"), rc_arch or "arm64",
                str(routing), str(actions), str(output),
                syscall_policy=True, hybrid=rc_arch is None, rc_kernel=rc_arch is not None,
                selected_buckets=unit_matrix.syscall_buckets(value) if value is not None else None,
                selected_owners=owners, mixed_rc_owners=mixed_rc_owners,
            )
            selection = json.loads(stdout.getvalue())
            self.assertEqual(output.read_text().splitlines(), selection["selected_owners"])
            return selection

    def combine(self, profiles, owners=None):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as stdout:
            for architecture, profile in profiles.items():
                (Path(directory) / f"{architecture}-selection.json").write_text(json.dumps(profile))
            unit_matrix.combine_rc_profiles(directory, owners)
            return json.loads(stdout.getvalue())

    def test_mixed_rc_subset_preserves_exact_union_and_routing(self):
        full = {architecture: self.select(None, rc_arch=architecture) for architecture in ("amd64", "arm64")}
        owners = [profile["selected_owners"][0] for profile in full.values()]
        # A mixed invocation may also select only one architecture's owners.
        for requested in (owners, owners[:1]):
            with self.subTest(owners=requested):
                profiles = {
                    architecture: self.select(None, owners=requested, rc_arch=architecture, mixed_rc_owners=True)
                    for architecture in full
                }
                combined = self.combine(profiles, requested)
                self.assertEqual(combined["selected_owners"], sorted(requested))
                self.assertEqual(combined["explicit_owners"], requested)
                self.assertEqual(sorted(requested + combined["unexecuted_owners"]), sorted(
                    label for profile in full.values() for label in profile["selected_owners"]
                ))
                for architecture, profile in profiles.items():
                    self.assertEqual(profile["canonical_owners"], full[architecture]["canonical_owners"])
                    self.assertEqual(profile["selected_owners"], sorted(set(requested) & set(full[architecture]["selected_owners"])))
                    self.assertEqual(profile["local_owners"], {
                        group: [owner for owner in labels if owner in requested]
                        for group, labels in full[architecture]["local_owners"].items()
                    })
                    self.assertEqual(profile["remote_owners"], [owner for owner in full[architecture]["remote_owners"] if owner in requested])

    def test_mixed_rc_union_rejects_missing_wrong_profile_and_extra_owners(self):
        owner = self.select(None, rc_arch="amd64")["selected_owners"][0]
        for invalid in ("//test/syscalls:missing_rc_tcg", owner.removesuffix("_rc_kvm") + "_arm64"):
            with self.subTest(owner=invalid):
                requested = [owner, invalid]
                profiles = {
                    architecture: self.select(None, owners=requested, rc_arch=architecture, mixed_rc_owners=True)
                    for architecture in ("amd64", "arm64")
                }
                with self.assertRaisesRegex(ValueError, "Combined RC profile changes explicit owners"):
                    self.combine(profiles, requested)
        profiles = {architecture: self.select(None, rc_arch=architecture) for architecture in ("amd64", "arm64")}
        with self.assertRaisesRegex(ValueError, "Combined RC profile changes explicit owners"):
            self.combine(profiles, [owner])
        for requested in ([], [owner, owner]):
            with self.subTest(owners=requested), self.assertRaisesRegex(ValueError, "nonempty and distinct"):
                self.select(None, owners=requested, rc_arch="amd64", mixed_rc_owners=True)
        with self.assertRaisesRegex(ValueError, "RC architecture selections overlap"):
            self.combine({"amd64": profiles["amd64"], "arm64": profiles["amd64"]})

    def test_mixed_rc_subset_requires_unselected_owner_routing(self):
        owner, unselected, *_ = self.select(None)["selected_owners"]
        with tempfile.TemporaryDirectory() as directory:
            graph = json.loads((FIXTURE / "actions.json").read_text())
            target_id, = [target["id"] for target in graph["targets"] if target["label"] == unselected]
            graph["actions"] = [action for action in graph["actions"] if action["targetId"] != target_id]
            actions = Path(directory) / "actions.json"
            actions.write_text(json.dumps(graph))
            for architecture, suffix in (("amd64", "_rc_kvm"), ("arm64", "_rc_tcg")):
                with self.subTest(architecture=architecture), self.assertRaisesRegex(ValueError, "Missing profile TestRunners"):
                    self.select(None, owners=[owner.removesuffix("_arm64") + suffix], rc_arch=architecture,
                                mixed_rc_owners=True, actions=actions)

    def test_explicit_owner_subset_preserves_profile_and_routing(self):
        for architecture in (None, "amd64", "arm64"):
            with self.subTest(rc_arch=architecture):
                full = self.select(None, rc_arch=architecture)
                owners = full["selected_owners"][:2]
                selected = self.select(None, owners=owners, rc_arch=architecture)
                self.assertEqual(selected["selected_owners"], owners)
                self.assertEqual(selected["explicit_owners"], owners)
                self.assertEqual(selected["canonical_owners"], full["canonical_owners"])
                self.assertEqual(sorted(owners + selected["unexecuted_owners"]), full["selected_owners"])
                self.assertEqual(selected["unexecuted_bucket_owners"], [])
                self.assertEqual(selected["local_owners"], {
                    group: [owner for owner in labels if owner in owners]
                    for group, labels in full["local_owners"].items()
                })
                self.assertEqual(selected["remote_owners"], [owner for owner in full["remote_owners"] if owner in owners])

    def test_explicit_owner_rejects_missing_wrong_architecture_and_wrong_profile(self):
        owner = self.select(None)["selected_owners"][0]
        for value in ("//test/syscalls:missing", owner.removesuffix("_arm64") + "_amd64", owner.removesuffix("_arm64") + "_rc_tcg"):
            with self.subTest(owner=value), self.assertRaisesRegex(ValueError, "outside the eligible syscall profile"):
                self.select(None, owners=[owner, value])

    def test_explicit_owner_rejects_empty_duplicate_and_bucket_combination(self):
        owner = self.select(None)["selected_owners"][0]
        for owners in ([], [owner, owner]):
            with self.subTest(owners=owners), self.assertRaisesRegex(ValueError, "nonempty and distinct"):
                self.select(None, owners=owners)
        with self.assertRaisesRegex(ValueError, "owners or buckets"):
            self.select("0", owners=[owner])

    def test_explicit_subset_still_requires_complete_profile_analysis(self):
        owner, unselected, *_ = self.select(None)["selected_owners"]
        with tempfile.TemporaryDirectory() as directory:
            events = [json.loads(line) for line in (FIXTURE / "routing.jsonl").read_text().splitlines()]
            routing = Path(directory) / "routing.jsonl"
            routing.write_text("".join(
                json.dumps(event) + "\n" for event in events
                if event.get("id", {}).get("targetConfigured", {}).get("label") != unselected
            ))
            with self.assertRaisesRegex(ValueError, "Configured owners differ from profile"):
                self.select(None, routing, owners=[owner])

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

    def test_explicit_workflow_input_rejected_before_bazel(self):
        root = Path(__file__).parents[2]
        environment = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
        owner = "--test-target=//test/syscalls:msync_test_runsc_kvm_rc_kvm"
        for arguments in (
            ["--arch=all", owner, "syscalls-rc"],
            ["--arch=all", "--test-target=--invalid", "test"],
            ["--arch=all", "--test-execution=local", owner, "test"],
            ["--arch=all", "--test-execution=local", "--syscall-bucket=0", owner, "syscalls-rc"],
            ["--arch=arm64", "--syscall-bucket=0", owner, "syscalls-rc"],
            ["--arch=arm64", owner, "syscalls"],
            ["--arch=arm64", owner, "unit"],
            ["--arch=arm64", "--test-target=--invalid", "syscalls-rc"],
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    ["bash", "test/rbe/qualify.sh", *arguments], cwd=root, env=environment,
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("bazel: command not found", result.stderr)

    @unittest.skipUnless(sys.platform == "linux", "The local benchmark route requires Linux")
    def test_continuous_benchmark_subset_keeps_local_docker_route(self):
        root = Path(__file__).parents[2]
        owners = [f"//test/benchmarks/fs:fio_test_continuous_{mode}_runc_owned"
                  for mode in ("randread", "randwrite", "read", "write")]
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            bazel = temporary / "bazel"
            bazel.write_text(f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys
if sys.argv[1] == 'query':
    print('\\n'.join(json.loads(os.environ['TEST_OWNERS'])))
elif sys.argv[1] == 'test':
    Path(os.environ['TEST_ARGUMENTS']).write_text(json.dumps(sys.argv[1:]))
else:
    raise SystemExit('Unexpected Bazel command')
""")
            bazel.chmod(0o755)
            arguments_path = temporary / "arguments.json"
            environment = {**os.environ, "PATH": str(temporary) + os.pathsep + os.environ["PATH"],
                           "RUNNER_TEMP": directory, "GVISOR_DOCKER_NETWORK": "fixture-network",
                           "TEST_OWNERS": json.dumps(owners), "TEST_ARGUMENTS": str(arguments_path)}
            for selected in (owners[:1], owners, [owners[0], "//outside:missing"]):
                with self.subTest(selected=selected):
                    arguments_path.unlink(missing_ok=True)
                    result = subprocess.run(
                        ["bash", "test/rbe/qualify.sh", "--arch=amd64", "--test-execution=local",
                         *["--benchmark-target=" + owner for owner in selected], "benchmarks"],
                        cwd=root, env=environment, capture_output=True, text=True, check=False, timeout=30,
                    )
                    if selected[-1] == "//outside:missing":
                        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        self.assertIn("Not a continuous benchmark target: //outside:missing", result.stderr)
                        self.assertFalse(arguments_path.exists())
                    else:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        arguments = json.loads(arguments_path.read_text())
                        self.assertEqual([arg for arg in arguments if arg.startswith("//")], selected)
                        self.assertIn("--strategy=TestRunner=docker", arguments)
                        self.assertIn("--run_under=//test/rbe:docker_setup", arguments)
                        self.assertIn("--test_env=GVISOR_DOCKER_NETWORK=fixture-network", arguments)
                        self.assertNotIn("--test_timeout", " ".join(arguments))
                        report = temporary / "qualification/benchmarks-selection"
                        self.assertEqual((report / "canonical-targets").read_text().splitlines(), owners)
                        self.assertEqual((report / "selected-targets").read_text().splitlines(), selected)


if __name__ == "__main__":
    unittest.main()
