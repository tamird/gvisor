"""Reconcile the shared verifier against retained native pass/failure evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest


class ImageResultControls(unittest.TestCase):
    fixture: dict
    bundles: Path
    output: Path

    def project(self, case: str, missing_bundle: bool = False) -> tuple[subprocess.CompletedProcess, dict]:
        selected = self.fixture[case]
        directory = self.output / self._testMethodName
        directory.mkdir()
        prefix = directory / "retained"
        metadata = directory / "metadata"
        metadata.mkdir()
        for name, body in selected.get("metadata_bytes", self.fixture["metadata_bytes"]).items():
            (metadata / (name + ".json")).write_text(body)
        if case == "nginx":
            fixture = directory / "fixture"
            fixture.mkdir()
            for name, body in selected["fixture_bytes"].items():
                (fixture / name).write_text(body)
            adapter = "nginx_result.py"
        else:
            fixture = directory / "spawn.js"
            fixture.write_text(self.fixture["script"])
            adapter = "bun_result.py"
        for suffix, value in selected["inputs"].items():
            Path(str(prefix) + "-" + suffix + ".json").write_text(json.dumps(value, indent=2) + "\n")
        # Only the local config reference moves; original source and bytes remain.
        config_path = Path(str(prefix) + "-config.json")
        plan_path = Path(str(prefix) + "-plan.json")
        plan = json.loads(plan_path.read_text())
        original = selected["original_prefix"] + "-config.json"
        [reference] = [row for row in plan["references"] if row["path"] == original]
        body = config_path.read_bytes()
        reference.update(path=str(config_path), bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
        plan_path.write_text(json.dumps(plan, indent=2) + "\n")
        if not missing_bundle:
            Path(str(prefix) + "-qualification.tar.gz").symlink_to(self.bundles / (case + ".tar.gz"))
        process = subprocess.run([
            sys.executable, str(Path(__file__).with_name(adapter)),
            str(prefix), str(metadata), str(fixture),
        ], capture_output=True, text=True, timeout=120)
        (directory / "stdout.log").write_text(process.stdout)
        (directory / "stderr.log").write_text(process.stderr)
        outcome = json.loads(Path(str(prefix) + "-outcome.json").read_text())
        return process, outcome

    def test_retained_pass_preserves_all_image_and_runtime_joins(self):
        process, outcome = self.project("pass")
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual(outcome["status"], "PASS")
        self.assertEqual(outcome["errors"], [])
        selected = self.fixture["pass"]
        stable = {key: outcome[key] for key in selected["expected_fields"]}
        actual = hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(actual, selected["expected_projection_sha256"])

    def test_retained_bytestream_pass_preserves_all_image_and_runtime_joins(self):
        process, outcome = self.project("nginx")
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertEqual((outcome["status"], outcome["native_status"], outcome["projection_status"]),
                         ("PASS", "PASS", "PASS"))
        self.assertEqual(outcome["errors"], [])
        selected = self.fixture["nginx"]
        stable = {key: outcome[key] for key in selected["expected_fields"]}
        actual = hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(actual, selected["expected_projection_sha256"])

    def test_retained_native_failure_remains_failure(self):
        process, outcome = self.project("fail")
        self.assertEqual(process.returncode, 1, process.stderr + process.stdout)
        self.assertEqual((outcome["status"], outcome["native_status"], outcome["projection_status"]),
                         ("FAIL", "FAIL", "PASS"))
        self.assertEqual(outcome["errors"], [])
        self.assertNotIn("runtime", outcome)

    def test_missing_bundle_writes_incomplete_and_exits_nonzero(self):
        process, outcome = self.project("pass", missing_bundle=True)
        self.assertEqual(process.returncode, 1, process.stderr + process.stdout)
        self.assertEqual((outcome["status"], outcome["native_status"], outcome["projection_status"]),
                         ("INCOMPLETE", "PASS", "INCOMPLETE"))
        self.assertTrue(outcome["errors"])
        self.assertNotIn("runtime", outcome)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundles", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    ImageResultControls.fixture = json.loads(Path(__file__).with_suffix(".json").read_text())
    ImageResultControls.bundles = args.bundles
    ImageResultControls.output = args.output
    result = unittest.main(argv=[sys.argv[0], "-v"], exit=False)
    return int(not result.result.wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())
