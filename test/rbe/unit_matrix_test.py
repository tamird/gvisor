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
        profile: Path = FIXTURE / "profile.jsonl",
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
            if owners is not None:
                # Model the narrowed second aquery, retaining the complete
                # first profile and its variant declarations independently.
                events = [json.loads(line) for line in routing.read_text().splitlines()]
                routing = Path(directory) / "selected-routing.jsonl"
                routing.write_text("".join(
                    json.dumps(event) + "\n" for event in events
                    if "targetConfigured" not in event["id"]
                    or event["id"]["targetConfigured"]["label"] in owners
                ))
                graph = json.loads(actions.read_text())
                selected_ids = {target["id"] for target in graph["targets"] if target["label"] in owners}
                graph["actions"] = [action for action in graph["actions"] if action["targetId"] in selected_ids]
                actions = Path(directory) / "selected-actions.json"
                actions.write_text(json.dumps(graph if selected_ids else {}))
            unit_matrix.select_profile(
                str(profile), rc_arch or "arm64",
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

    def test_mixed_rc_subset_does_not_claim_unselected_routing(self):
        owner, unselected, *_ = self.select(None)["selected_owners"]
        with tempfile.TemporaryDirectory() as directory:
            graph = json.loads((FIXTURE / "actions.json").read_text())
            target_id, = [target["id"] for target in graph["targets"] if target["label"] == unselected]
            graph["actions"] = [action for action in graph["actions"] if action["targetId"] != target_id]
            actions = Path(directory) / "actions.json"
            actions.write_text(json.dumps(graph))
            for architecture, suffix in (("amd64", "_rc_kvm"), ("arm64", "_rc_tcg")):
                with self.subTest(architecture=architecture):
                    selected = self.select(None, owners=[owner.removesuffix("_arm64") + suffix], rc_arch=architecture,
                                           mixed_rc_owners=True, actions=actions)
                    self.assertEqual(selected["routing_unchecked_owners"], selected["unexecuted_owners"])
                    self.assertIn(unselected.removesuffix("_arm64") + suffix, selected["routing_unchecked_owners"])

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
                self.assertEqual(selected["routing_unchecked_owners"], selected["unexecuted_owners"])
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

    def test_explicit_query_analyzes_only_requested_owners(self):
        owner = self.select(None)["selected_owners"][0]
        command = [sys.executable, str(Path(unit_matrix.__file__)), "profile-actions", str(FIXTURE / "profile.jsonl"), "arm64", "--hybrid"]
        result = subprocess.run(command + [f"--owner={owner}"], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), f'mnemonic("^TestRunner$", set("{owner}"))')
        full = subprocess.run(command, capture_output=True, text=True, check=True)
        for label in self.select(None)["selected_owners"]:
            self.assertIn(f'"{label}"', full.stdout)
        for owners in ([owner, owner], [owner.removesuffix("_arm64") + "_amd64"]):
            with self.subTest(owners=owners):
                result = subprocess.run(command + [f"--owner={value}" for value in owners], capture_output=True, text=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_explicit_subset_still_checks_all_canonical_variant_declarations(self):
        owner, unselected, *_ = self.select(None)["selected_owners"]
        with tempfile.TemporaryDirectory() as directory:
            events = [json.loads(line) for line in (FIXTURE / "profile.jsonl").read_text().splitlines()]
            for event in events:
                if event.get("id", {}).get("targetConfigured", {}).get("label") == unselected.removesuffix("_arm64"):
                    event["configured"]["tag"].remove("rbe-has-arm64-variant")
            profile = Path(directory) / "profile.jsonl"
            profile.write_text("".join(json.dumps(event) + "\n" for event in events))
            with self.assertRaisesRegex(ValueError, "Profile owners without _arm64 variants"):
                self.select(None, owners=[owner], profile=profile)

    def test_explicit_subset_rejects_missing_or_invalid_selected_route(self):
        owner = self.select(None)["selected_owners"][0]
        for defect in ("missing", "configuration", "placement"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                graph = json.loads((FIXTURE / "actions.json").read_text())
                target_id, = [target["id"] for target in graph["targets"] if target["label"] == owner]
                action, = [action for action in graph["actions"] if action["targetId"] == target_id]
                if defect == "missing":
                    graph["actions"].remove(action)
                elif defect == "configuration":
                    action["configurationId"] = "wrong-configuration"
                    graph["configuration"].append({"id": "wrong-configuration", "checksum": "wrong-checksum"})
                else:
                    for item in action["executionInfo"]:
                        if item["key"] == "Arch":
                            item["value"] = "amd64"
                actions = Path(directory) / "actions.json"
                actions.write_text(json.dumps(graph))
                message = {"missing": "Missing profile TestRunners", "configuration": "Unexpected TestRunner configuration", "placement": "Unexpected native test routing"}[defect]
                with self.assertRaisesRegex(ValueError, message):
                    self.select(None, owners=[owner], actions=actions)

    def test_empty_subset_still_requires_successful_analysis(self):
        owner = self.select(None, rc_arch="amd64")["selected_owners"][0]
        with tempfile.TemporaryDirectory() as directory:
            events = [json.loads(line) for line in (FIXTURE / "routing.jsonl").read_text().splitlines()]
            for event in events:
                if "finished" in event:
                    event["finished"]["overallSuccess"] = False
            routing = Path(directory) / "routing.jsonl"
            routing.write_text("".join(json.dumps(event) + "\n" for event in events))
            with self.assertRaisesRegex(ValueError, "Expected successful"):
                self.select(None, routing, owners=[owner], rc_arch="arm64", mixed_rc_owners=True)

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
            ["--arch=all", owner, "test"],
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


if __name__ == "__main__":
    unittest.main()
