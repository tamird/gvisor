"""Test producer joins against both CPUs from the retained FSstress invocation."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from image_execution import program_producer


class ProgramProducerTest(unittest.TestCase):
    roles = {
        "bin/busybox": ("//images:busybox_build", "CcMakeMakeRule"),
        "usr/bin/fsstress": ("@@+_repo_rules+ltp//:fsstress", "CppLink"),
    }
    fixture = json.loads(Path(__file__).with_name("image_execution_fixture.json").read_bytes())

    def setUp(self) -> None:
        row = deepcopy(self.fixture["architectures"]["x86_64"])
        self.capture = row["capture"]
        [self.program] = [p for p in row["programs"] if p["installed_path"] == "bin/busybox"]
        [self.action] = [a for a in self.capture["image_actions"] if a["targetLabel"] == "//images:busybox_build"]
        [self.output] = [o for o in self.action["actualOutputs"] if o["path"] == self.program["path"]]

    def match(self, capture: dict, cpu: str, program: dict) -> dict:
        return program_producer(capture, cpu, "busybox", *self.roles[program["installed_path"]],
                                path=program["path"], size=program["size"], sha256=program["sha256"])

    def test_retained_remote_builds_and_cache_hits(self) -> None:
        self.assertEqual(set(self.fixture["architectures"]), {"x86_64", "aarch64"})
        for cpu, row in self.fixture["architectures"].items():
            self.assertCountEqual([p["installed_path"] for p in row["programs"]], self.roles)
            for p in row["programs"]:
                name = p["installed_path"]
                with self.subTest(architecture=cpu, program=name):
                    producer = self.match(row["capture"], cpu, p)
                    self.assertEqual(producer["output"], {"path": p["path"], "bytes": p["size"], "sha256": p["sha256"]})
                    cached = name == "usr/bin/fsstress"
                    self.assertEqual((producer["runner"], producer["cache_hit"]), ("remote cache hit" if cached else "remote", cached))
                    [action] = [a for a in row["capture"]["image_actions"] if a["targetLabel"] == self.roles[name][0]]
                    self.assertEqual(producer["action_sha256"], action["digest"]["hash"])
                    self.assertEqual(producer["full_record_sha256"], action["full_record_sha256"])

    def test_capture_scope(self) -> None:
        for change in ({"status": "INCOMPLETE"}, {"status": "FAIL"}, {"phase": "other"}, {"architecture": "aarch64"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "execution log"):
                self.match({**self.capture, **change}, "x86_64", self.program)

    def test_missing_or_ambiguous_producer(self) -> None:
        for actions in ([], [self.action, self.action], [{**self.action, "mnemonic": "CppLink"}],
                        [{**self.action, "targetLabel": "//images:busybox_binary"}],
                        [{**self.action, "actualOutputs": []}]):
            with self.subTest(actions=len(actions)), self.assertRaisesRegex(ValueError, "Expected one"):
                self.match({**self.capture, "image_actions": actions}, "x86_64", self.program)

    def test_output_path_digest_and_length(self) -> None:
        for change in ({"path": "elsewhere/" + self.program["path"]}, {"sha256": "0" * 64},
                       {"size": self.program["size"] + 1}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "Expected one|differs"):
                self.match(self.capture, "x86_64", {**self.program, **change})
        self.output["digest"].pop("sizeBytes")
        with self.assertRaisesRegex(ValueError, "with a digest"):
            self.match(self.capture, "x86_64", self.program)

    def test_failed_or_incomplete_action(self) -> None:
        for change in ({"exitCode": 1}, {"status": "CANCELLED"}, {"remotable": False},
                       {"digest": {}}, {"full_record_sha256": ""}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "successfully|record digest"):
                self.match({**self.capture, "image_actions": [{**self.action, **change}]}, "x86_64", self.program)

    def test_local_or_inconsistent_cache_provenance(self) -> None:
        for runner, cached in (("local", False), ("remote", True), ("remote cache hit", False), ("disk cache hit", True)):
            with self.subTest(runner=runner, cached=cached), self.assertRaisesRegex(ValueError, "remote producer"):
                action = {**self.action, "runner": runner, "cacheHit": cached}
                self.match({**self.capture, "image_actions": [action]}, "x86_64", self.program)


if __name__ == "__main__":
    unittest.main(verbosity=2)
