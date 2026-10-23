"""Check native variant attribution against two authenticated compiler runs."""

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile
import time
import unittest
import urllib.request

# The owning driver supplies its already authenticated reporter directory.
reporter_directory, evidence_directory = sys.argv[1:]
sys.path.insert(0, reporter_directory)
from image_workload import bind_built_outputs, declared_variants


def retained_archive(name: str, supplier: dict) -> tarfile.TarFile:
    """Retrieve only the fixed compact bundle; never a full execution audit."""
    expected = supplier["artifact"]
    request = urllib.request.Request(
        "https://app.buildbuddy.io/api/v1/GetFile",
        data=json.dumps({"uri": expected["uri"]}).encode(),
        headers={"Content-Type": "application/json",
                 "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    deadline = float(os.environ["APPLICABILITY_WORK_DEADLINE"])
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Retained variant evidence deadline exhausted")
    path = Path(evidence_directory) / (name + "-compiler-evidence.tar.gz")
    size = 0
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=min(60, remaining)) as response, path.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > expected["bytes"] or time.monotonic() >= deadline:
                raise ValueError("Retained variant evidence exceeded size or time bound")
            digest.update(chunk)
            output.write(chunk)
    if (size, digest.hexdigest()) != (expected["bytes"], expected["sha256"]):
        raise ValueError("Retained variant archive identity differs")
    return tarfile.open(path)


def member(archive: tarfile.TarFile, name: str) -> dict:
    match [entry for entry in archive.getmembers() if entry.name == name]:
        case [entry] if entry.isfile():
            return json.load(archive.extractfile(entry))
        case _:
            raise ValueError("Expected one regular retained member: " + name)


class NativeVariantsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
        variants = declared_variants(cls.config["build_groups"])
        cls.datasets = {}
        for name, supplier in cls.config["variant_suppliers"].items():
            with retained_archive(name, supplier) as archive:
                record = member(archive, "compiler-current-build.json")
                if (record["source"], record["status"]) != (supplier["source"], supplier["producer_status"]):
                    raise ValueError("Retained producer source/status differs")
                groups = []
                for configured in cls.config["build_groups"]:
                    match [group for group in record["builds"] if group["name"] == configured["name"]]:
                        case [group] if group["status"] == group["build_status"] == "PASS":
                            pass
                        case _:
                            raise ValueError("Missing retained successful build group")
                    phase = group.get("phase", group["name"])
                    native = member(archive, "invocation-" + phase + "/verdict.json")
                    if (native["source"], native["status"], native["native_exit"]) != (supplier["source"], "PASS", 0):
                        raise ValueError("Retained native build did not pass")
                    proof_name = ("multi-" if name == "current" else group["architecture"] + "-") + phase + "-image-execution.json"
                    group = copy.deepcopy(group)
                    if name == "old":
                        group["roots"] = {target: target for target in group["targets"]}
                        group["declared_variants"] = {
                            target: variants[root] for root, target in configured["roots"].items()
                            if root in variants}
                    elif group["roots"] != configured["roots"]:
                        raise ValueError("Current wrapper/source mapping differs")
                    if set(group["targets"]) != set(configured["targets"]):
                        raise ValueError("Retained build target set differs")
                    groups.append((group, member(archive, proof_name)))
                if name == "old":
                    # Later phases reuse outputs whose producer is recorded in
                    # an earlier phase of this same authenticated invocation.
                    if any(proof["status"] != "PASS" for _, proof in groups):
                        raise ValueError("Retained native action evidence did not complete")
                    proof = {"status": "PASS", "image_actions": [
                        action for _, phase_proof in groups for action in phase_proof["image_actions"]]}
                    groups = [(group, proof) for group, _ in groups]
                cls.datasets[name] = groups

    def test_current_all_forty(self) -> None:
        groups = [group for group, _ in self.datasets["current"]]
        outputs = copy.deepcopy([output for group in groups for output in group["outputs"]])
        variants = declared_variants(groups)
        self.assertEqual(len(outputs), 40)
        self.assertEqual({value: list(variants.values()).count(value) for value in set(variants.values())},
                         {"race": 10, "pure": 14, "native": 4})
        bind_built_outputs(outputs, self.config["expected_outputs"], self.datasets["current"][0][1], variants)
        self.assertEqual(sum(output["variant"].get("race", False) for output in outputs), 10)

    def test_old_build_success_does_not_grant_race_coverage(self) -> None:
        rows = []
        for group, proof in self.datasets["old"]:
            # Preserve this old invocation's source labels and full paths.
            variants = group["declared_variants"]
            outputs = copy.deepcopy(group["outputs"])
            expected = {output["identity"]["label"]: output["name"] for output in outputs}
            if group["name"].endswith("-race"):
                with self.assertRaisesRegex(ValueError, "Native GoLink variant"):
                    bind_built_outputs(outputs, expected, proof, variants)
            else:
                bind_built_outputs(outputs, expected, proof, variants)
            for output in group["outputs"]:
                label = output["identity"]["label"]
                required_race = variants.get(label) == "race"
                if required_race:
                    with self.assertRaisesRegex(ValueError, "Native GoLink variant"):
                        bind_built_outputs([copy.deepcopy(output)], {label: output["name"]}, proof, variants)
                else:
                    bind_built_outputs([copy.deepcopy(output)], {label: output["name"]}, proof, variants)
                rows.append({"group": group["name"], "target": label, "native_build_status": "PASS",
                             "required_race": required_race,
                             "race_coverage": "INVALID" if required_race else "NOT_APPLICABLE"})
        self.assertEqual(len(rows), 40)
        self.assertEqual(sum(row["required_race"] for row in rows), 10)
        (Path(evidence_directory) / "prior-variant-coverage.json").write_text(json.dumps(rows, indent=2) + "\n")

    def test_missing_and_unknown_variant_rejected(self) -> None:
        groups = [group for group, _ in self.datasets["current"]]
        variants = declared_variants(groups)
        output = next(output for group in groups for output in group["outputs"]
                      if variants.get(output["identity"]["label"]) == "race")
        label = output["identity"]["label"]
        for mapping, message in (({}, "Missing declared variant"), ({label: "raec"}, "Unknown declared")):
            with self.subTest(mapping=mapping), self.assertRaisesRegex(ValueError, message):
                bind_built_outputs([copy.deepcopy(output)], {label: output["name"]},
                                   self.datasets["current"][0][1], mapping)

    def test_incomplete_native_proof_rejected(self) -> None:
        proof = copy.deepcopy(self.datasets["current"][0][1])
        proof["status"] = "INCOMPLETE"
        with self.assertRaisesRegex(ValueError, "Native action evidence did not complete"):
            bind_built_outputs([], {}, proof, {})


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
