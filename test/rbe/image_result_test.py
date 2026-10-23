"""Exercise supplemental producer joins against the actual retained bundles."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest
import urllib.request

# The driver supplies the same authenticated reporter used by its own verdicts.
private, evidence = map(Path, sys.argv[1:])
del sys.argv[1:]
sys.path.insert(0, str(private))

from image_archive_test import ConfiguredInputsTest, ExecutableAliasesTest
from image_result import ImageBundle, bind_image_outputs, load_retained_bundle


def fetch(expected: dict, destination: Path) -> None:
    """Reuse the existing bounded GetFile contract for ordinary bundles only."""
    def remaining() -> float:
        value = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
        if value <= 0:
            raise TimeoutError("Retained result control deadline exhausted")
        return value

    request = urllib.request.Request(
        "https://app.buildbuddy.io/api/v1/GetFile",
        data=json.dumps({"uri": expected["uri"]}).encode(),
        headers={"Content-Type": "application/json",
                 "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    size, digest = 0, hashlib.sha256()
    with urllib.request.urlopen(request, timeout=min(60, remaining())) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            remaining()
            size += len(chunk)
            if size > expected["bytes"]:
                raise ValueError("Retained bundle exceeded its declared size")
            digest.update(chunk)
            output.write(chunk)
    if (size, digest.hexdigest()) != (expected["bytes"], expected["sha256"]):
        raise ValueError("Retained bundle identity differs")


class SupplementalProducerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])["retained_result_controls"]
        cls.receipt = cls.config["original_receipt"]
        recovery = cls.config["recovery_receipt"]
        assert cls.receipt["status"] == cls.receipt["producer_status"] == "PASS"
        assert recovery["status"] == recovery["producer_status"] == "PASS"
        cls.bundles = []
        for name, receipt in (("original", cls.receipt), ("recovery", recovery)):
            path = private / (name + "-result-control.tar.gz")
            fetch(receipt["native_invocation"]["artifact"], path)
            [source] = receipt["sources"]
            cls.bundles.append(load_retained_bundle(receipt, source, path))
        cls.bundle, recovered = cls.bundles
        assert set(recovered.phases) == {
            "invocation-00-buildifier/verdict.json",
            "invocation-retained-ltp-actions/verdict.json"}
        assert all(row["status"] == "PASS" and row["native_exit"] == 0
                   for row in recovered.phases.values())
        selected = json.loads(recovered.members["retained-ltp-actions.json"])
        assert selected["status"] == "PASS" and selected["source"] == recovered.source
        assert selected["supplier"]["source"] == cls.bundle.source
        assert selected["supplier"]["artifact"] == cls.receipt["native_invocation"]["artifact"]
        assert selected["supplier"]["invocation"] == cls.receipt["native_invocation"]["invocationId"]
        assert selected["supplier"]["audits"] == cls.receipt["audit_artifacts"]
        cls.actions = [{**row, "recovery": recovery["native_invocation"]} for row in selected["actions"]]
        contents = json.loads(cls.bundle.members["root-image-contents.json"])
        cls.images = {name: image["architectures"] for name, image in contents["images"].items()}
        helper = Path(__file__).with_name("image_result.py")
        assert hashlib.sha256(helper.read_bytes()).hexdigest() == cls.config["helper_sha256"]

    def bind(self, actions: list[dict], bundle: ImageBundle | None = None) -> tuple[list, list, list]:
        return bind_image_outputs(
            bundle or self.bundle, self.receipt, self.config["original_config"], self.images,
            {"busybox": 1, "symlink-resolv": 1, "fsstress": 2},
            private / "retained-result-control", supplemental_actions=actions)

    def test_actual_supplemented_result(self) -> None:
        outputs, programs, audits = self.bind(self.actions)
        self.assertEqual((len(outputs), len(programs), len(audits)), (12, 8, 2))
        self.assertEqual(sum("supplemental_recovery" in row for row in programs), 2)

    def test_wrong_source_or_output(self) -> None:
        for field, value in (("source", "0" * 40), ("invocation", "foreign"),
                             ("path", "bazel-out/foreign"), ("sha256", "0" * 64), ("bytes", 1)):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.actions)
                destination = changed[0] if field in ("source", "invocation") else changed[0]["output"]
                destination[field] = value
                with self.assertRaises((AssertionError, ValueError)):
                    self.bind(changed)

    def test_duplicate_producer(self) -> None:
        with self.assertRaisesRegex(ValueError, "Expected one producing action"):
            self.bind([*self.actions, copy.deepcopy(self.actions[0])])

    def test_missing_or_zero_producers(self) -> None:
        for actions in (self.actions[:1], []):
            with self.subTest(count=len(actions)):
                with self.assertRaisesRegex(ValueError, "Expected one producing action"):
                    self.bind(actions)

    def test_failed_producer(self) -> None:
        for field, value in (("exitCode", 1), ("status", "FAILED")):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.actions)
                changed[0]["action"][field] = value
                with self.assertRaises(AssertionError):
                    self.bind(changed)

    def test_incomplete_original_proof(self) -> None:
        members = dict(self.bundle.members)
        name = "x86_64-busybox-image-execution.json"
        proof = json.loads(members[name])
        proof["status"] = "INCOMPLETE"
        members[name] = json.dumps(proof).encode()
        changed = ImageBundle(self.bundle.source, members, self.bundle.index, self.bundle.phases)
        with self.assertRaises(AssertionError):
            self.bind(self.actions, changed)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    record = {"status": "PASS" if result.wasSuccessful() else "FAIL",
              "tests": result.testsRun, "failures": len(result.failures),
              "errors": len(result.errors), "scope": "Retained real-bundle reporting controls; no runtime replay."}
    (evidence / "retained-result-controls.json").write_text(json.dumps(record, indent=2) + "\n")
    raise SystemExit(int(not result.wasSuccessful()))
