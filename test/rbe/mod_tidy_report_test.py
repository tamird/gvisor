"""Check module-command results without weakening source and lock evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import qualification_report as report


class ModuleCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        commit = b"tree " + b"0" * 40 + b"\n\nModule command fixture\n"
        source = hashlib.sha1(b"commit " + str(len(commit)).encode() + b"\0" + commit).hexdigest()
        values = {"commit": commit, "before": b"", "after": b"", "head": source.encode(),
                  "native-exit": b"0", "capture-exit": b"0", "lock-before": b"lock",
                  "lock-after": b"lock", "stdout.log": b"", "stderr.log": b""}
        for name, value in values.items():
            (self.root / name).write_bytes(value)
        context = {"expected_source": source, "command_kind": "command",
                   "execution_finished_at": "2026-10-23T00:00:00+00:00",
                   "commit_file": "commit", "source_before": "before", "source_after": "after",
                   "final_head": "head", "executed_arguments": "arguments.json",
                   "native_exit": "native-exit", "capture_exit": "capture-exit",
                   "lock_before": "lock-before", "lock_after": "lock-after"}
        (self.root / "context.json").write_text(json.dumps(context))
        self.arguments("tidy")

    def arguments(self, subcommand: str) -> None:
        (self.root / "arguments.json").write_text(json.dumps(
            ["--bazelrc=/fixture/credentials.bazelrc", "mod", subcommand,
             "--downloader_config=/fixture/downloader", "--lockfile_mode=error"]))

    def verdict(self) -> dict[str, object]:
        return report.command_verdict(self.root / "context.json")

    def test_supported_commands(self) -> None:
        for subcommand in ("deps", "tidy"):
            with self.subTest(subcommand=subcommand):
                self.arguments(subcommand)
                value = self.verdict()
                self.assertEqual(value["status"], "PASS", value)
                self.assertEqual(value["errors"], [])

    def test_tidy_source_change_is_incomplete(self) -> None:
        (self.root / "after").write_text(" M MODULE.bazel\n")
        value = self.verdict()
        self.assertEqual(value["status"], "INCOMPLETE", value)
        self.assertTrue(any("Tracked source is dirty" in error for error in value["errors"]))

    def test_tidy_lock_change_is_incomplete(self) -> None:
        (self.root / "lock-after").write_text("changed")
        value = self.verdict()
        self.assertEqual(value["status"], "INCOMPLETE", value)
        self.assertIn("Lock evidence is missing or changed", value["errors"])

    def test_native_tidy_failure_is_preserved(self) -> None:
        (self.root / "native-exit").write_text("2")
        value = self.verdict()
        self.assertEqual(value["status"], "FAIL", value)
        self.assertEqual(value["native_exit"], 2)

    def test_other_module_command_is_rejected(self) -> None:
        self.arguments("graph")
        value = self.verdict()
        self.assertEqual(value["status"], "INCOMPLETE", value)
        self.assertTrue(any("original Bazel mod" in error for error in value["errors"]))


if __name__ == "__main__":
    unittest.main()
