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
import base64
import contextlib
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import unit_matrix


FIXTURE = Path(__file__).parent / "testdata/syscall_buckets"


class DispatchPlanTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manifest = Path(self.directory.name) / "pgo.json"
        self.manifest.write_text(json.dumps({"pgo": {
            "//bench:first_systrap": {"platform": "systrap"},
            "//bench:second_systrap": {"platform": "systrap"},
            "//bench:first_kvm": {"platform": "kvm"},
        }}))
        # The shell remains the lane-name authority; planning must reject a
        # selector that the actual entrypoint does not recognize.
        source = Path(__file__).with_name("qualify.sh").read_text()
        declaration = next(line for line in source.splitlines() if line.startswith("lanes=("))
        self.lanes = shlex.split(declaration.removeprefix("lanes=(").removesuffix(")"))
        catalog = b'[{"name":"cos-fixture","family":"cos-stable"}]'
        self.inputs = dict(
            profile="public-cpu", source="a" * 40, run_id="1234", run_attempt=2,
            lanes=["smoke"], architecture="amd64", execution="remote",
            requirements="namespace", targets=[], test_filter="", syscall_bucket="",
            header_base="b" * 40, cos_sha256=hashlib.sha256(catalog).hexdigest(),
            cos_gzip_base64=base64.b64encode(gzip.compress(catalog)).decode(),
            clock_source="calibrated", known_lanes=self.lanes, pgo_manifest=self.manifest,
        )

    def plan(self, **changes):
        return unit_matrix.dispatch_plan(**(self.inputs | changes))

    def test_selected_checks_publish_before_nogo_on_the_same_machine(self) -> None:
        plan = self.plan(profile="single", lanes=["nogo", "smoke", "workflows"])
        self.assertEqual([(row["lanes"], row["runner"]) for row in plan["rows"]],
                         [(["smoke", "workflows"], "ubuntu-24.04"), (["nogo"], "ubuntu-24.04")])

    def test_full_routes_preserve_native_hosts_and_canonical_pgo_selection(self):
        plan = self.plan()
        rows = {row["id"]: row for row in plan["rows"]}
        self.assertEqual(len(rows), len(plan["rows"]))
        self.assertEqual(rows["all-local-unit"]["runner"], "ubuntu-24.04-arm")
        self.assertEqual(rows["all-local-syscalls-rc"]["runner"], "ubuntu-26.04")
        self.assertEqual(rows["arm64-local-smoke"]["lanes"], ["smoke"])
        self.assertEqual(rows["amd64-local-container"]["local_test_requirements"], "kvm")
        self.assertEqual(rows["amd64-remote-pgo"]["targets"], ["//bench:first_systrap", "//bench:second_systrap"])
        self.assertEqual(rows["amd64-local-pgo"]["targets"], ["//bench:first_kvm"])
        selected = {lane for row in rows.values() for lane in row["lanes"]}
        self.assertEqual(selected & {"container-v1-pilot", "syscalls-rc-pilot", "license-fetch"}, set())
        self.assertEqual(rows["all-remote-startup-posture-benchmarks"]["required_kinds"], ["test"])
        self.assertEqual(rows["amd64-remote-pgo"]["required_kinds"], ["benchmark"])
        self.assertEqual(rows["all-remote-nogo-workflows-gazelle-lint-lint-cc-governance"]["required_kinds"], ["build", "command", "test"])

    def test_single_and_full_share_routes_without_expanding_work_deadlines(self):
        full = {row["id"]: row for row in self.plan()["rows"]}
        for lane, arch, mode, budget in (
            ("docker", "amd64", "remote", (2700, 70, 80)),
            ("license-check", "amd64", "remote", (2700, 70, 80)),
            ("kvm", "arm64", "remote", (2700, 70, 80)),
            ("benchmarks", "amd64", "local", (15000, 275, 285)),
            ("unit", "all", "local", (7200, 145, 155)),
            ("syscalls-rc", "all", "local", (5400, 115, 120)),
        ):
            with self.subTest(lane=lane, arch=arch, mode=mode):
                row, = self.plan(profile="single", lanes=[lane], architecture=arch, execution=mode)["rows"]
                self.assertEqual(row, full[row["id"]])
                self.assertEqual(tuple(row[key] for key in ("work_seconds", "step_minutes", "job_minutes")), budget)

    def test_full_plan_rejects_missing_or_changed_side_inputs_and_narrowing(self):
        for changes, message in (
            ({"header_base": ""}, "License headers require the full comparison SHA"),
            ({"cos_gzip_base64": ""}, "COS metadata requires the complete encoded catalog and its SHA256"),
            ({"cos_sha256": "0" * 64}, "COS catalog SHA256 does not match its decoded bytes"),
            ({"targets": ["//pkg:test"]}, "The public CPU profile cannot narrow targets, cases or syscall buckets"),
            ({"test_filter": "OneCase"}, "The public CPU profile cannot narrow targets, cases or syscall buckets"),
            ({"syscall_bucket": "0"}, "The public CPU profile cannot narrow targets, cases or syscall buckets"),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError) as error:
                self.plan(**changes)
            self.assertEqual(str(error.exception), message)

    def test_dispatch_identity_requires_positive_exact_types(self):
        for changes in ({"run_id": "0"}, {"run_id": ""}, {"run_id": "１２"},
                        {"run_attempt": 0}, {"run_attempt": True}, {"run_attempt": "2"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError) as error:
                self.plan(**changes)
            self.assertEqual(str(error.exception),
                             "Dispatch requires a full source SHA, positive decimal run ID and positive integer attempt")

    def test_qualifier_plans_without_entering_linux_workload_path(self):
        environment = os.environ | {
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
            "QUALIFICATION_PROFILE": "single", "QUALIFICATION_COMMIT": "a" * 40,
            "GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "2",
            "QUALIFICATION_LANES": "docker", "QUALIFICATION_ARCH": "amd64",
            "QUALIFICATION_EXECUTION": "remote", "QUALIFICATION_CLOCK_SOURCE": "reference",
            "QUALIFICATION_LOCAL_TEST_REQUIREMENTS": "namespace", "QUALIFICATION_TARGETS": "",
            "QUALIFICATION_TEST_FILTER": "TestExample", "QUALIFICATION_SYSCALL_BUCKET": "",
            "QUALIFICATION_HEADER_BASE": "", "QUALIFICATION_COS_SHA256": "",
            "QUALIFICATION_COS_GZIP_BASE64": "",
        }
        result = subprocess.run(
            ["/bin/bash", str(Path(__file__).with_name("qualify.sh").resolve()), "--dispatch-plan"],
            env=environment, cwd=self.directory.name, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.plan(
            profile="single", lanes=["docker"], test_filter="TestExample", clock_source="reference",
            header_base="", cos_sha256="", cos_gzip_base64="",
        ))

    def test_running_row_is_bound_to_source_attempt_selection_and_side_inputs(self):
        plan = self.plan(profile="single", lanes=["docker"], test_filter="TestExample")
        row, = plan["rows"]
        directory = Path(self.directory.name)
        plan_path, output = directory / "plan.json", directory / "matrix-row.json"
        plan_path.write_text(json.dumps(plan) + "\n")
        environment = {
            "QUALIFICATION_COMMIT": plan["source"], "GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "2",
            "QUALIFICATION_LANES": "docker", "QUALIFICATION_ARCH": "amd64",
            "QUALIFICATION_EXECUTION": "remote-actions", "QUALIFICATION_TARGETS": "",
            "QUALIFICATION_TEST_FILTER": "TestExample", "QUALIFICATION_SYSCALL_BUCKET": "",
            "QUALIFICATION_LOCAL_TEST_REQUIREMENTS": "namespace", "QUALIFICATION_HEADER_BASE": "b" * 40,
            "QUALIFICATION_COS_SHA256": self.inputs["cos_sha256"], "QUALIFICATION_CLOCK_SOURCE": "calibrated",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            unit_matrix.dispatch_row_metadata(plan_path, row["id"], output)
            result = json.loads(output.read_text())
            self.assertEqual(result["row"], row)
            self.assertEqual(result["plan_sha256"], hashlib.sha256(plan_path.read_bytes()).hexdigest())
            original = output.read_bytes()
            for name in ("QUALIFICATION_COMMIT", "GITHUB_RUN_ATTEMPT", "QUALIFICATION_TEST_FILTER", "QUALIFICATION_COS_SHA256"):
                with self.subTest(input=name), mock.patch.dict(os.environ, {name: "changed"}):
                    with self.assertRaises(ValueError) as error:
                        unit_matrix.dispatch_row_metadata(plan_path, row["id"], output)
                    self.assertEqual(str(error.exception), f"Coordinator input {name} differs from dispatch plan")
                    self.assertEqual(output.read_bytes(), original)
            for name in ("QUALIFICATION_TARGETS", "QUALIFICATION_SYSCALL_BUCKET"):
                with self.subTest(missing=name), mock.patch.dict(os.environ):
                    del os.environ[name]
                    with self.assertRaises(ValueError) as error:
                        unit_matrix.dispatch_row_metadata(plan_path, row["id"], output)
                    self.assertEqual(str(error.exception), f"Coordinator input {name} differs from dispatch plan")
                    self.assertEqual(output.read_bytes(), original)


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
            ["--arch=arm64", "--test-filter=TestOnly", "cpu-images"],
            ["--arch=arm64", "--test-target=--invalid", "syscalls-rc"],
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    ["bash", "test/rbe/qualify.sh", *arguments], cwd=root, env=environment,
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("bazel: command not found", result.stderr)

    @unittest.skipUnless(sys.platform == "linux", "The local Docker routes require Linux")
    def test_local_docker_routes_keep_selection_and_source_placement(self):
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
            uname = temporary / "uname"
            uname.write_text(f"""#!{sys.executable}
import os
import sys
if sys.argv[1:] == ['-m']:
    print(os.environ['TEST_HOST_ARCH'])
elif sys.argv[1:] == ['-s']:
    print('Linux')
else:
    raise SystemExit('Unexpected uname arguments')
""")
            uname.chmod(0o755)
            arguments_path = temporary / "arguments.json"
            environment = {**os.environ, "PATH": str(temporary) + os.pathsep + os.environ["PATH"],
                           "RUNNER_TEMP": directory, "GVISOR_DOCKER_NETWORK": "fixture-network",
                           "TEST_HOST_ARCH": "x86_64",
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

            compose_filter = "^TestDockerWith(VFS|Overlay)$/^docker_compose_build_host_network$"
            for architecture, execution, lane, test_filter in (
                ("arm64", "local", "docker", ""),
                ("arm64", "local", "docker", compose_filter),
                ("amd64", "remote", "docker", compose_filter),
                ("amd64", "remote", "test", compose_filter),
                ("arm64", "local", "cpu-images", ""),
                ("arm64", "local", "gpu-images", ""),
                ("amd64", "local", "cpu-images", ""),
                ("arm64", "remote", "cpu-images", ""),
                ("amd64", "remote", "cpu-images", ""),
            ):
                with self.subTest(architecture=architecture, execution=execution, lane=lane, test_filter=test_filter):
                    arguments_path.unlink(missing_ok=True)
                    environment["TEST_HOST_ARCH"] = "aarch64" if architecture == "arm64" else "x86_64"
                    target = f"//test/rbe:gcsfuse_{architecture}_test"
                    if lane in ("docker", "test"):
                        target = "//test/image:image_test_owned"
                    selected = ["--test-target=" + target] if lane in ("cpu-images", "docker", "test") else []
                    result = subprocess.run(
                        ["bash", "test/rbe/qualify.sh", "--arch=" + architecture,
                         "--test-execution=" + execution, "--test-filter=" + test_filter, *selected, lane],
                        cwd=root, env=environment, capture_output=True, text=True, check=False, timeout=30,
                    )
                    if architecture == "amd64" and execution == "local":
                        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                        self.assertIn("Local tests support explicit test targets", result.stderr)
                        self.assertFalse(arguments_path.exists())
                        continue
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    arguments = json.loads(arguments_path.read_text())
                    self.assertEqual([arg for arg in arguments if arg.startswith("--test_filter=")],
                                     ["--test_filter=" + test_filter] if test_filter else [])
                    self.assertEqual("--strategy=SourceImageOCI=local" in arguments,
                                     architecture == "arm64" and execution == "local")
                    self.assertIn("--config=rbe-arm64" if architecture == "arm64" else "--config=rbe", arguments)
                    self.assertFalse(any(arg.startswith("--spawn_strategy=") for arg in arguments))
                    if selected:
                        self.assertEqual([arg for arg in arguments if arg.startswith("//")],
                                         [target])
                    if execution == "local":
                        self.assertIn("--strategy=TestRunner=docker", arguments)
                        self.assertIn("--run_under=//test/rbe:docker_setup", arguments)
                        self.assertIn("--test_env=GVISOR_DOCKER_NETWORK=fixture-network", arguments)
                    self.assertNotIn("--test_timeout", " ".join(arguments))


if __name__ == "__main__":
    unittest.main()
