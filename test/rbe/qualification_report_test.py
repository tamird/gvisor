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

"""Reconcile small projections of retained qualification results."""

from __future__ import annotations

import datetime
import errno
import hashlib
from http.client import RemoteDisconnected
import io
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
from typing import BinaryIO, TextIO
import unittest
from unittest import mock
import zipfile

import collect_actions
import qualification_report as report


FIXTURES = Path(__file__).with_name("testdata") / "qualification_report"
LIFECYCLE_MANIFEST = Path(__file__).parent.parent / "benchmarks" / "lifecycle.json"


class QualificationReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()

    def fixture(self, name: str = "pass") -> Path:
        invocation = self.evidence / ("invocation-" + name)
        shutil.copytree(FIXTURES / name, invocation)
        raw = invocation / "events.jsonl"
        raw.write_text(raw.read_text().replace("file:///FIXTURE_OUTPUT/", invocation.as_uri() + "/"))
        return invocation

    def capture(self, invocation: Path, retain_outputs: bool = False) -> dict[str, object]:
        return report.capture(invocation / "events.jsonl", invocation / "captured.jsonl",
                              invocation, invocation / "reports", retain_outputs)

    def verdict(self, invocation: Path, *, analysis: Path | None = None, benchmarks: Path | None = None,
                declared_benchmarks: Path | None = None, case_manifest: Path | None = None) -> dict[str, object]:
        events = invocation / "captured.jsonl"
        value = report.verdict(events, report.manifest_path(events), invocation / "context.json",
                               invocation / "expected.txt", (analysis,) if analysis else (), benchmarks,
                               declared_benchmarks, case_manifest)
        report.write_json(invocation / "verdict.json", value)
        return value

    def events(self, invocation: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in (invocation / "events.jsonl").read_text().splitlines()]

    def write_events(self, invocation: Path, events: list[dict[str, object]]) -> None:
        (invocation / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))

    def analysis(self, invocation: Path) -> Path:
        path = invocation / "expected-analysis.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in self.events(invocation)
                                if "configured" in event or "finished" in event))
        return path

    def mixed_fixture(self, xml: str) -> Path:
        invocation = self.fixture()
        events = self.events(invocation)
        label = (invocation / "expected.txt").read_text().strip()
        second_label = "//another:test"
        second = invocation / "another"
        second.mkdir()
        (second / "test.xml").write_text(xml)
        shutil.copyfile(invocation / "test.log", second / "test.log")
        extra = json.dumps([event for event in events if "finished" not in event])
        extra = extra.replace(label, second_label).replace(invocation.as_uri() + "/test.", second.as_uri() + "/test.")
        self.write_events(invocation, json.loads(extra) + events)
        (invocation / "expected.txt").write_text(label + "\n" + second_label + "\n")
        return invocation

    def test_retained_pass(self) -> None:
        invocation = self.fixture()
        self.assertEqual(self.capture(invocation)["errors"], [])
        value = self.verdict(invocation, analysis=self.analysis(invocation))
        self.assertEqual(value["status"], "PASS", value)
        self.assertEqual(value["case_counts"], {"passed": 45, "failed": 0, "skipped": 4})
        self.assertEqual(value["missing"], [])
        self.assertEqual(len(value["executed"]), 1)

    def test_required_cases_must_pass_for_their_own_target(self) -> None:
        invocation = self.mixed_fixture('<testsuite tests="1"><testcase name="Only"/></testsuite>')
        self.assertEqual(self.capture(invocation)["errors"], [])
        first, second = (invocation / "expected.txt").read_text().splitlines()
        manifest = invocation / "required.json"
        report.write_json(manifest, {first: ["ElfTest/Execute"], second: ["Only"]})
        self.assertEqual(self.verdict(invocation, case_manifest=manifest)["status"], "PASS")

        # The first case is skipped. The second passes, but on the wrong target.
        report.write_json(manifest, {first: ["ElfTest/PIEOutOfOrderSegments"], second: ["ElfTest/Execute"]})
        value = self.verdict(invocation, case_manifest=manifest)
        self.assertEqual(value["status"], "INCOMPLETE")
        self.assertEqual({(item["label"], item["name"]) for item in value["missing"]},
                         {(second, "ElfTest/Execute")})
        skipped, = value["unpassed_required_cases"]
        self.assertEqual((skipped["label"], skipped["name"], skipped["outcomes"]),
                         (first, "ElfTest/PIEOutOfOrderSegments", ["skipped"]))
        report.write_json(manifest, {first: [skipped["name"]]})
        value = self.verdict(invocation, case_manifest=manifest)
        self.assertEqual((value["status"], value["missing"]), ("INCOMPLETE", []))
        (invocation / "native-exit").write_text("3\n")
        self.assertEqual(self.verdict(invocation, case_manifest=manifest)["status"], "FAIL")

    def source_image_fixture(self) -> Path:
        invocation = self.evidence / "source-images"
        shutil.copytree(FIXTURES / "source-images", invocation)
        return invocation

    def source_image_audit(self, invocation: Path) -> dict[str, object]:
        return report.source_image_audit(invocation / "execution.json", invocation / "verdict.json",
                                         invocation / "manifest.json", invocation / "expectations.json")

    def native_image_fixture(self) -> Path:
        invocation = self.source_image_fixture()
        image, = self.source_image_audit(invocation)["images"]
        records, spawns = report.compact_records(invocation / "execution.json")
        source, = [spawn for spawn in spawns if spawn.get("mnemonic") == "SourceImageOCI"]
        layout_id, = [output["outputId"] for output in source["outputs"]
                      if "directory" in records[output["outputId"]]]
        layout = records[layout_id]["directory"]["path"]
        base_id, layer_id, descriptor_id, inputs_id = range(max(records) + 1, max(records) + 5)
        base = "bazel-out/k8-fastbuild/bin/external/base/layout"
        layer = "bazel-out/k8-fastbuild/bin/images/helper/layer.tar"
        descriptor = "bazel-out/k8-fastbuild/bin/images/helper/image.0.descriptor.json"
        records.update({
            base_id: {"id": base_id, "directory": {"path": base}},
            layer_id: {"id": layer_id, "file": {"path": layer, "digest": {"hash": "b" * 64, "sizeBytes": "100"}}},
            descriptor_id: {"id": descriptor_id, "file": {"path": descriptor, "digest": {"hash": "c" * 64, "sizeBytes": "80"}}},
            inputs_id: {"id": inputs_id, "inputSet": {"inputIds": [base_id, layer_id, descriptor_id]}},
        })
        # This is the retained rules_oci OCIImage argv/output shape. Its build
        # inputs may be cached; the test attempt and observed load remain real.
        source.update(mnemonic="OCIImage", inputSetId=inputs_id,
                      args=["bazel-out/k8-fastbuild/bin/images/helper/image_image.sh",
                            "--from=" + base, "--layer=" + layer + "=" + descriptor],
                      outputs=[{"outputId": layout_id}])
        (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in
            [*records.values(), *({"spawn": spawn} for spawn in spawns)]))
        canonical = report.read_json(invocation / "verdict.json")
        owner, = canonical["expected"]
        contract = report.read_json(invocation / "expectations.json")
        contract["configured_owners"] = [{**owner, "images": [{"name": image["image"], "architecture": "amd64",
            "archive": image["archive"]["path"], "origin": "native-oci", "layout": layout}], "source_commands": []}]
        report.write_json(invocation / "expectations.json", contract)
        return invocation

    def test_native_images_bind_layout_inputs_archive_and_actual_load(self) -> None:
        for cached, kind in ((False, "benchmark"), (True, "benchmark"), (True, "build")):
            with self.subTest(cached=cached, kind=kind):
                invocation = self.native_image_fixture()
                try:
                    records, spawns = report.compact_records(invocation / "execution.json")
                    source, = [spawn for spawn in spawns if spawn.get("mnemonic") == "OCIImage"]
                    source.update(cacheHit=cached, runner="remote cache hit" if cached else "remote")
                    (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in
                        [*records.values(), *({"spawn": spawn} for spawn in spawns)]))
                    if kind == "build":
                        canonical = report.read_json(invocation / "verdict.json")
                        canonical.update(command_kind="build", executed=[])
                        report.write_json(invocation / "verdict.json", canonical)
                        contract = report.read_json(invocation / "expectations.json")
                        contract.pop("benchmarks")
                        report.write_json(invocation / "expectations.json", contract)
                    value = self.source_image_audit(invocation)
                    self.assertEqual((value["audit_status"], value["errors"]), ("PASS", []), value)
                    image, = value["images"]
                    self.assertEqual((image["origin"], image["producer"]["cache_hit"]), ("native-oci", cached))
                    self.assertEqual([row["kind"] for row in image["producer"]["inputs"]], ["base", "layer", "descriptor"])
                    self.assertEqual(len(value["consumers"]), int(kind != "build"))
                finally:
                    shutil.rmtree(invocation)

    def test_native_images_reject_missing_or_conflicting_ancestry(self) -> None:
        for damage, error in (("layout", "Missing or ambiguous native OCI layout"),
                              ("assembly", "Missing unique native OCI assembly"),
                              ("failed", "Unsuccessful source/archive action"),
                              ("layer", "is not an assembly input"),
                              ("archive", "Source archive identities differ"),
                              ("registry", "Registry dependency requires")):
            with self.subTest(damage=damage):
                invocation = self.native_image_fixture()
                try:
                    contract = report.read_json(invocation / "expectations.json")
                    image, = contract["configured_owners"][0]["images"]
                    records, spawns = report.compact_records(invocation / "execution.json")
                    source, = [spawn for spawn in spawns if spawn.get("mnemonic") == "OCIImage"]
                    if damage == "layout":
                        image["layout"] += "/different-configuration"
                    elif damage == "assembly":
                        spawns.append(source.copy())
                    elif damage == "failed":
                        source["exitCode"] = 1
                    elif damage == "layer":
                        records[source["inputSetId"]]["inputSet"]["inputIds"].pop(1)
                    elif damage == "archive":
                        image["archive"] += ".unrelated"
                    else:
                        image["origin"] = "registry"
                    report.write_json(invocation / "expectations.json", contract)
                    (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in
                        [*records.values(), *({"spawn": spawn} for spawn in spawns)]))
                    value = self.source_image_audit(invocation)
                    self.assertEqual(value["audit_status"], "INCOMPLETE", value)
                    self.assertIn(error, "\n".join(value["errors"]))
                finally:
                    shutil.rmtree(invocation)

    def registry_image_fixture(self) -> Path:
        invocation = self.native_image_fixture()
        contract = report.read_json(invocation / "expectations.json")
        owner, = contract["configured_owners"]
        image, = owner["images"]
        records, spawns = report.compact_records(invocation / "execution.json")
        source, = [spawn for spawn in spawns if spawn.get("mnemonic") == "OCIImage"]
        layout_id, = [item["outputId"] for item in source["outputs"]]
        repository = "rules_oci++oci+fixture_linux_amd64"
        image.update(origin="registry", layout="bazel-out/k8-fastbuild/bin/external/" + repository + "/layout",
                     pin="bazel-out/k8-fastbuild/bin/owner.source-image-pin-0", usage="docker-load", config="",
                     index="bazel-out/k8-fastbuild/bin/owner.source-image-index-0.json")
        pin = "e" * 64
        owner["pins"] = {image["pin"]: {"digest": "sha256:" + pin}}
        body = json.dumps({"schemaVersion": 2, "manifests": [{"digest": "sha256:" + pin, "size": 100,
                           "mediaType": "application/vnd.oci.image.manifest.v1+json"}]})
        owner["indexes"] = {image["index"]: {"body": body, "evidence": {
            "sha256": hashlib.sha256(body.encode()).hexdigest(), "bytes": len(body.encode())}}}
        files = [{"path": path, "digest": {"hash": digest, "sizeBytes": "100"}} for path, digest in (
            ("blobs/sha256/" + pin, pin), ("index.json", "f" * 64), ("oci-layout", "a" * 64))]
        files[1]["digest"] = {"hash": hashlib.sha256(body.encode()).hexdigest(), "sizeBytes": str(len(body.encode()))}
        records[layout_id]["directory"] = {"path": image["layout"], "files": files}
        first = max(records) + 1
        for identifier, file in enumerate(files, first):
            records[identifier] = {"id": identifier, "file": {**file, "path": "external/" + repository + "/" + file["path"]}}
        inputs_id = first + len(files)
        records[inputs_id] = {"id": inputs_id, "inputSet": {"inputIds": list(range(first, inputs_id))}}
        source.update(mnemonic="CopyToDirectory", inputSetId=inputs_id,
                      args=["copy_to_directory", "configured-copy.json", repository], cacheHit=True, runner="remote cache hit")
        report.write_json(invocation / "expectations.json", contract)
        (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in
            [*records.values(), *({"spawn": spawn} for spawn in spawns)]))
        return invocation

    def test_registry_archive_binds_existing_pin_and_copy_inputs(self) -> None:
        for damage, error in ((None, None), ("shared", None), ("pin", "Registry index does not select the configured manifest pin"),
                              ("unselected", "Registry index does not select the configured manifest pin"),
                              ("index-bytes", "Registry index differs from the configured layout"),
                              ("member", "Registry layout member differs"), ("ancestry", "not a layout copy input"),
                              ("copy", "Missing unique registry layout copy"), ("archive", "Source archive identities differ")):
            with self.subTest(damage=damage):
                invocation = self.registry_image_fixture()
                try:
                    contract = report.read_json(invocation / "expectations.json")
                    owner, = contract["configured_owners"]
                    records, spawns = report.compact_records(invocation / "execution.json")
                    source, = [spawn for spawn in spawns if spawn.get("mnemonic") == "CopyToDirectory"]
                    if damage == "shared":
                        canonical = report.read_json(invocation / "verdict.json")
                        second_owner = {**canonical["expected"][0], "label": canonical["expected"][0]["label"] + "_second"}
                        canonical["expected"].append(second_owner)
                        second_attempt = json.loads(json.dumps(canonical["executed"][0]))
                        second_attempt["owner"] = second_owner
                        canonical["executed"].append(second_attempt)
                        report.write_json(invocation / "verdict.json", canonical)
                        contract["owners"][second_owner["label"]] = contract["owners"][owner["label"]]
                        contract["benchmarks"][second_owner["label"]] = contract["benchmarks"][owner["label"]]
                        second = json.loads(json.dumps(owner))
                        second["label"] = second_owner["label"]
                        image = second["images"][0]
                        old_index = image["index"]
                        image["index"] = old_index.replace("owner.", "second-owner.")
                        second["indexes"] = {image["index"]: second["indexes"][old_index]}
                        contract["configured_owners"].append(second)
                        test, = [spawn for spawn in spawns if spawn.get("mnemonic") == "TestRunner"]
                        clone = json.loads(json.dumps(test))
                        clone["targetLabel"] = second_owner["label"]
                        for output in clone["outputs"]:
                            if "invalidOutputPath" in output:
                                output["invalidOutputPath"] = output["invalidOutputPath"].replace("_owned/", "_owned_second/")
                                continue
                            identifier = output["outputId"]
                            record = json.loads(json.dumps(records[identifier]))
                            new_id = max(records) + 1
                            record["id"] = new_id
                            match record:
                                case {"file": entry} | {"directory": entry}:
                                    entry["path"] = entry["path"].replace("_owned/", "_owned_second/")
                                case _:
                                    self.fail(f"Unexpected retained output record: {record}")
                            records[new_id] = record
                            output["outputId"] = new_id
                        spawns.append(clone)
                        manifest = report.read_json(invocation / "manifest.json")
                        for original in list(manifest["reports"]):
                            row = json.loads(json.dumps(original))
                            row["identity"]["label"] = second_owner["label"]
                            row["path"] = row["path"].replace("_owned/", "_owned_second/")
                            destination = invocation / row["path"]
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(invocation / original["path"], destination)
                            manifest["reports"].append(row)
                        report.write_json(invocation / "manifest.json", manifest)
                    elif damage == "pin":
                        next(iter(owner["pins"].values()))["digest"] = "sha256:" + "0" * 64
                    elif damage in ("unselected", "index-bytes"):
                        index = next(iter(owner["indexes"].values()))
                        value = json.loads(index["body"])
                        if damage == "unselected":
                            value["manifests"][0]["digest"] = "sha256:" + "0" * 64
                        else:
                            value["annotations"] = {"changed": "index content"}
                        index["body"] = json.dumps(value)
                        index["evidence"] = {"sha256": hashlib.sha256(index["body"].encode()).hexdigest(),
                                             "bytes": len(index["body"].encode())}
                    elif damage == "member":
                        first = records[source["inputSetId"]]["inputSet"]["inputIds"][0]
                        records[first]["file"]["digest"] = {"hash": "1" * 64, "sizeBytes": "100"}
                    elif damage == "ancestry":
                        records[source["inputSetId"]]["inputSet"]["inputIds"].pop()
                    elif damage == "copy":
                        spawns.append(source.copy())
                    elif damage == "archive":
                        owner["images"][0]["archive"] += ".unrelated"
                    report.write_json(invocation / "expectations.json", contract)
                    (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in
                        [*records.values(), *({"spawn": spawn} for spawn in spawns)]))
                    value = self.source_image_audit(invocation)
                    self.assertEqual(value["audit_status"], "INCOMPLETE" if error else "PASS", value)
                    if error:
                        self.assertIn(error, "\n".join(value["errors"]))
                    else:
                        image, = value["images"]
                        self.assertEqual((image["origin"], image["producer"]["cache_hit"]), ("registry", True))
                        self.assertEqual(image["producer"]["pin"], "sha256:" + "e" * 64)
                        self.assertEqual(len(image["producer"]["inputs"]), 3)
                        self.assertEqual(len(value["consumers"]), 2 if damage == "shared" else 1)
                        self.assertEqual(len(image["producer"]["indexes"]), 2 if damage == "shared" else 1)
                finally:
                    shutil.rmtree(invocation)

    def cri_image_fixture(self) -> Path:
        invocation = self.source_image_fixture()
        image, = self.source_image_audit(invocation)["images"]
        canonical = report.read_json(invocation / "verdict.json")
        owner, = canonical["expected"]
        config = "bazel-out/k8-fastbuild/bin/test/root/image_config.json"
        short = config.split("/bin/", 1)[1]
        contract = report.read_json(invocation / "expectations.json")
        contract["configured_owners"] = [{**owner, "source_commands": [], "images": [{
            "name": image["image"], "architecture": image["architecture"], "archive": image["archive"]["path"],
            "origin": "source", "layout": "", "pin": "", "index": "", "usage": "cri-import", "config": config}]}]
        report.write_json(invocation / "expectations.json", contract)
        body = json.dumps({"containerd_version": "1.7.31", "sandbox_image": image["image"],
                           "images": {image["image"]: image["archive"]["path"].split("/bin/", 1)[1]}})
        records, spawns = report.compact_records(invocation / "execution.json")
        test, = [spawn for spawn in spawns if spawn.get("mnemonic") == "TestRunner"]
        config_id, inputs_id = range(max(records) + 1, max(records) + 3)
        records[config_id] = {"id": config_id, "file": {"path": config, "digest": {
            "hash": hashlib.sha256(body.encode()).hexdigest(), "sizeBytes": str(len(body.encode()))}}}
        records[inputs_id] = {"id": inputs_id, "inputSet": {"inputIds": [config_id], "transitiveSetIds": [test["inputSetId"]]}}
        test["inputSetId"] = inputs_id
        test["args"] += ["--harness_image_config=" + short, "--containerd_version=1.7.31"]
        (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in
            [*records.values(), *({"spawn": spawn} for spawn in spawns)]))
        manifest = report.read_json(invocation / "manifest.json")
        log, = [row for row in manifest["reports"] if row["name"] == "test.log"]
        path = invocation / log["path"]
        path.write_text(path.read_text() + "\nStaged declared CRI images: " + json.dumps({"path": short, "config": body}) +
                        "\nImported declared CRI image " + image["image"] + " from /cri-images/" +
                        image["image"].replace("/", "_") + ".tar\n")
        log.update(report.file_record(path))
        report.write_json(invocation / "manifest.json", manifest)
        return invocation

    def test_cri_imports_require_declared_roles_config_and_success_observations(self) -> None:
        for damage, error in ((None, None), ("staged", None), ("missing", "Checked CRI imports differ"),
                              ("staged-only", "Checked CRI imports differ"), ("extra", "Unexpected checked CRI import"),
                              ("config", "Staged CRI config bytes differ"), ("argument", "does not select"),
                              ("archive", "Loaded source archives differ")):
            with self.subTest(damage=damage):
                invocation = self.cri_image_fixture()
                try:
                    contract = report.read_json(invocation / "expectations.json")
                    records, spawns = report.compact_records(invocation / "execution.json")
                    test, = [spawn for spawn in spawns if spawn.get("mnemonic") == "TestRunner"]
                    manifest = report.read_json(invocation / "manifest.json")
                    log, = [row for row in manifest["reports"] if row["name"] == "test.log"]
                    path = invocation / log["path"]
                    if damage in ("missing", "staged"):
                        path.write_text(path.read_text().replace("Imported declared CRI image", "Failed CRI import"))
                        if damage == "staged":
                            contract["configured_owners"][0]["images"][0]["usage"] = "staged-only"
                    elif damage == "staged-only":
                        contract["configured_owners"][0]["images"][0]["usage"] = "staged-only"
                    elif damage == "extra":
                        path.write_text(path.read_text() + "Imported declared CRI image unknown from /cri-images/unknown.tar\n")
                    elif damage == "config":
                        config_id, = records[test["inputSetId"]]["inputSet"]["inputIds"]
                        records[config_id]["file"]["digest"]["hash"] = "0" * 64
                    elif damage == "argument":
                        test["args"] = [argument for argument in test["args"] if not argument.startswith("--harness_image_config=")]
                    elif damage == "archive":
                        records[45644]["inputSet"]["inputIds"] = []
                    log.update(report.file_record(path))
                    report.write_json(invocation / "manifest.json", manifest)
                    report.write_json(invocation / "expectations.json", contract)
                    (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in
                        [*records.values(), *({"spawn": spawn} for spawn in spawns)]))
                    value = self.source_image_audit(invocation)
                    self.assertEqual(value["audit_status"], "INCOMPLETE" if error else "PASS", value)
                    if error:
                        self.assertIn(error, "\n".join(value["errors"]))
                    else:
                        consumer, = value["consumers"]
                        self.assertEqual(consumer["cri"]["imports"], {} if damage == "staged" else {"benchmarks/iperf": 1})
                        self.assertEqual(consumer["images"][0]["usage"], "staged-only" if damage == "staged" else "cri-import")
                finally:
                    shutil.rmtree(invocation)

    def test_source_images_bind_local_runfiles_archive_and_measurements(self) -> None:
        invocation = self.source_image_fixture()
        value = self.source_image_audit(invocation)
        self.assertEqual((value["status"], value["audit_status"], value["errors"]), ("PASS", "PASS", []), value)
        image, = value["images"]
        consumer, = value["consumers"]
        loaded, = consumer["images"]
        self.assertEqual(image["producer"]["runner"], "remote")
        self.assertEqual((consumer["execution"]["runner"], consumer["execution"]["digest"]), ("docker", None))
        self.assertEqual(loaded["input_chain"], [45655, 45654, 45653, 45652, 45651, 45647, 45645, 45644, 45563])
        self.assertEqual(loaded["archive"]["digest"], {
            "hash": "48350c832feea207a0b685ac577ba689bbbe437352949ca658fb65a46218bc6c", "sizeBytes": "30893056"})

    def test_source_images_reject_missing_or_ambiguous_evidence(self) -> None:
        for damage, expected_error in (
                ("load", "Missing actual archive load"),
                ("other-archive", "Missing actual archive load"),
                ("runfiles", "Loaded source images differ"),
                ("xml", "TestRunner XML differs"),
                ("configuration", "Missing or duplicate canonical test attempts"),
                ("duplicate-attempt", "Missing or duplicate canonical test attempts"),
                ("duplicate-spawn", "Duplicate or mismatched configured TestRunner"),
                ("missing-spawn", "Missing configured TestRunners"),
                ("missing-image", "Source producers differ"),
                ("required-case", "Measured benchmark names differ"),
                ("cached-source", "Source image was cached"),
                ("remote-no-digest", "Missing execution identity")):
            with self.subTest(damage=damage):
                invocation = self.source_image_fixture()
                try:
                    records, spawns = report.compact_records(invocation / "execution.json")
                    source, = [spawn for spawn in spawns if spawn["mnemonic"] == "SourceImageOCI"]
                    test, = [spawn for spawn in spawns if spawn["mnemonic"] == "TestRunner"]
                    manifest = report.read_json(invocation / "manifest.json")
                    canonical = report.read_json(invocation / "verdict.json")
                    contract = report.read_json(invocation / "expectations.json")
                    owner, = contract["owners"]
                    if damage in ("load", "other-archive", "xml"):
                        name = "test.xml" if damage == "xml" else "test.log"
                        row, = [row for row in manifest["reports"] if row["name"] == name]
                        path = invocation / row["path"]
                        text = path.read_text()
                        if damage == "load":
                            text = text.replace("Loaded declared image archive", "Unobserved archive")
                        elif damage == "other-archive":
                            text = text.replace("Loaded image:", "Loaded declared image archive other/tarball.tar:\nLoaded image:", 1)
                        else:
                            text += "\n"
                        path.write_text(text)
                        row.update(report.file_record(path))
                    elif damage == "runfiles":
                        records[45644]["inputSet"]["inputIds"] = []
                    elif damage == "configuration":
                        canonical["executed"][0]["owner"]["configuration"] = "other-configuration"
                    elif damage == "duplicate-attempt":
                        canonical["executed"].append(canonical["executed"][0])
                    elif damage == "duplicate-spawn":
                        spawns.append(test)
                    elif damage == "missing-spawn":
                        spawns.remove(test)
                    elif damage == "missing-image":
                        contract["owners"][owner].append("benchmarks/missing")
                    elif damage == "required-case":
                        contract["benchmarks"][owner].append({"name": "BenchmarkMissing", "count": 1, "units": ["ns/op"]})
                    elif damage == "cached-source":
                        source["cacheHit"] = True
                    elif damage == "remote-no-digest":
                        test["runner"] = "remote"
                    report.write_json(invocation / "manifest.json", manifest)
                    report.write_json(invocation / "verdict.json", canonical)
                    report.write_json(invocation / "expectations.json", contract)
                    (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in
                        [*records.values(), *({"spawn": spawn} for spawn in spawns)]))
                    value = self.source_image_audit(invocation)
                    self.assertEqual((value["status"], value["canonical_status"]), ("INCOMPLETE", "PASS"), value)
                    self.assertIn(expected_error, "\n".join(value["errors"]))
                finally:
                    shutil.rmtree(invocation)

    def test_source_images_preserve_failure_and_reject_partial_measurements(self) -> None:
        invocation = self.source_image_fixture()
        canonical = report.read_json(invocation / "verdict.json")
        canonical.update(status="FAIL", native_exit=7)
        report.write_json(invocation / "verdict.json", canonical)
        value = self.source_image_audit(invocation)
        self.assertEqual((value["status"], value["native_exit"], value["audit_status"]), ("FAIL", 7, "PASS"))
        manifest = report.read_json(invocation / "manifest.json")
        row, = [row for row in manifest["reports"] if row["name"] == "test.log"]
        log = invocation / row["path"]
        log.write_text(log.read_text().replace("PASS", "FAIL"))
        row.update(report.file_record(log))
        report.write_json(invocation / "manifest.json", manifest)
        value = self.source_image_audit(invocation)
        self.assertEqual((value["status"], value["native_exit"], value["audit_status"]), ("FAIL", 7, "INCOMPLETE"))
        self.assertIn("Benchmark log has a Go package FAIL marker", value["errors"])
        (invocation / "execution.json").unlink()
        value = self.source_image_audit(invocation)
        self.assertEqual((value["status"], value["native_exit"], value["audit_status"]), ("FAIL", 7, "INCOMPLETE"))

    def test_source_images_share_producer_without_pooling_archive_inputs(self) -> None:
        invocation = self.source_image_fixture()
        records, spawns = report.compact_records(invocation / "execution.json")
        # A second configured conversion consumes the same source outputs.
        # Its archive is not an input of this retained TestRunner.
        conversion_ids = set(range(45556, 45564)) - {45557}
        copies = []
        for identifier in sorted(conversion_ids):
            row = json.loads(json.dumps(records[identifier]))
            row["id"] += 100000
            if "inputSet" in row:
                for field in ("inputIds", "transitiveSetIds"):
                    if field in row["inputSet"]:
                        row["inputSet"][field] = [value + 100000 if value in conversion_ids else value
                                                  for value in row["inputSet"][field]]
            if "file" in row:
                row["file"]["path"] = row["file"]["path"].replace("source_load/", "other_source_load/")
            copies.append(row)
        for spawn in spawns:
            if spawn["mnemonic"] in ("OCITarballManifest", "OCITarball"):
                row = json.loads(json.dumps(spawn))
                row["inputSetId"] += 100000
                row["outputs"] = [{"outputId": output["outputId"] + 100000} for output in row["outputs"]]
                copies.append({"spawn": row})
        with (invocation / "execution.json").open("a") as stream:
            stream.write("\n".join(json.dumps(row) for row in copies))
        value = self.source_image_audit(invocation)
        self.assertEqual(value["errors"], [], value)
        self.assertEqual(len(value["images"]), 2)
        consumer, = value["consumers"]
        loaded, = consumer["images"]
        self.assertEqual(loaded["input_chain"][-1], 45563)
        records[45644]["inputSet"]["inputIds"] = [145563]
        (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in
            [*records.values(), *({"spawn": spawn} for spawn in spawns), *copies]))
        value = self.source_image_audit(invocation)
        self.assertEqual(value["status"], "INCOMPLETE", value)
        self.assertIn("Missing actual archive load", "\n".join(value["errors"]))

    def test_source_images_cannot_complete_incomplete_or_unrun_workloads(self) -> None:
        invocation = self.source_image_fixture()
        canonical = report.read_json(invocation / "verdict.json")
        canonical["status"] = "INCOMPLETE"
        report.write_json(invocation / "verdict.json", canonical)
        value = self.source_image_audit(invocation)
        self.assertEqual((value["status"], value["canonical_status"], value["audit_status"]),
                         ("INCOMPLETE", "INCOMPLETE", "PASS"))
        # Even a falsely successful verdict with no test attempts cannot
        # acquire coverage from otherwise complete image/load evidence.
        canonical.update(status="PASS", executed=[])
        report.write_json(invocation / "verdict.json", canonical)
        value = self.source_image_audit(invocation)
        self.assertEqual((value["status"], value["audit_status"]), ("INCOMPLETE", "INCOMPLETE"))
        self.assertIn("Missing or duplicate canonical test attempts", value["errors"])

    def test_source_image_decoder_failure_preserves_native_failure(self) -> None:
        invocation = self.source_image_fixture()
        canonical = report.read_json(invocation / "verdict.json")
        canonical.update(status="FAIL", native_exit=7)
        report.write_json(invocation / "verdict.json", canonical)
        value = report.audit_source_images(invocation / "execution.json", self.root / "missing-bb",
                                           invocation / "verdict.json", invocation / "manifest.json",
                                           invocation / "expectations.json")
        self.assertEqual((value["status"], value["native_exit"], value["audit_status"]), ("FAIL", 7, "INCOMPLETE"))
        with mock.patch.object(report.subprocess, "run", side_effect=subprocess.CalledProcessError(
                2, ["bb", "print"], stderr="Malformed compact execution log")):
            value = report.audit_source_images(invocation / "execution.json", invocation / "execution.json",
                                               invocation / "verdict.json", invocation / "manifest.json",
                                               invocation / "expectations.json")
        self.assertEqual((value["status"], value["native_exit"]), ("FAIL", 7))
        self.assertIn("Malformed compact execution log", "\n".join(value["errors"]))

    def test_source_images_bind_generated_xml_to_its_exact_test_log(self) -> None:
        invocation = self.source_image_fixture()
        records, spawns = report.compact_records(invocation / "execution.json")
        test, = [spawn for spawn in spawns if spawn["mnemonic"] == "TestRunner"]
        xml_path = records[45656]["file"]["path"]
        test["outputs"] = [{"invalidOutputPath": xml_path} if output.get("outputId") == 45656 else output
                           for output in test["outputs"]]
        generator = {"mnemonic": "TestRunner", "targetLabel": test["targetLabel"],
                     "args": ["external/bazel_tools/tools/test/generate-xml.sh",
                              xml_path.removesuffix("test.xml") + "test.log", xml_path],
                     "outputs": [{"outputId": 45656}]}
        spawns.append(generator)
        for suffix, expected_status in (("", "PASS"), (".different-shard", "INCOMPLETE")):
            with self.subTest(suffix=suffix):
                generator["args"][1] += suffix
                (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in
                    [*records.values(), *({"spawn": spawn} for spawn in spawns)]))
                value = self.source_image_audit(invocation)
                self.assertEqual(value["status"], expected_status, value)

    def build_output_fixture(self) -> tuple[Path, Path, Path]:
        invocation = self.fixture()
        path = invocation / "analysis.sarif"
        path.write_text('{"runs": []}\n')
        identity = report.file_record(path)
        events = [event for event in self.events(invocation) if "testResult" not in event and "testSummary" not in event]
        for event in events:
            event["children"] = [child for child in event.get("children", []) if "targetCompleted" in child]
            if "configured" in event:
                event["configured"].pop("testSize", None)
                event["configured"]["targetKind"] = "filegroup rule"
                event["configured"]["tag"] = ["qualification-output-group=default"]
            if "completed" in event:
                event["completed"]["outputGroup"] = [{"name": "default", "fileSets": [{"id": "parent"}]}]
        # Real BEP uses string lengths and can put a referenced file set after
        # its owner. Include a transitive set as emitted for aggregate reports.
        events.extend([
            {"id": {"namedSet": {"id": "parent"}}, "namedSetOfFiles": {"fileSets": [{"id": "leaf"}]}},
            {"id": {"namedSet": {"id": "leaf"}}, "namedSetOfFiles": {"files": [
                {"name": path.name, "uri": path.as_uri(), "digest": identity["sha256"], "length": str(identity["bytes"])},
            ]}},
        ])
        self.write_events(invocation, events)
        context = report.read_json(invocation / "context.json")
        context["command_kind"] = "build"
        report.write_json(invocation / "context.json", context)
        (invocation / "executed-arguments.txt").write_text("build\n//:report\n")
        return invocation, self.analysis(invocation), path

    def test_build_outputs_preserve_failures_and_reject_missing_or_corrupt_evidence(self) -> None:
        for damage, expected_status, error in (
            (None, "PASS", None),
            ("missing-file", "INCOMPLETE", "No such file"),
            ("digest", "INCOMPLETE", "advertised digest/size"),
            ("missing-group", "INCOMPLETE", "Missing build output groups"),
            ("missing-set", "INCOMPLETE", "missing build output file set"),
            ("cycle", "INCOMPLETE", "Cyclic"),
            ("empty", "INCOMPLETE", "Empty build output group"),
            ("changed-copy", "INCOMPLETE", "digest/size mismatch"),
            ("failed-build", "FAIL", "Missing build output groups"),
        ):
            with self.subTest(damage=damage):
                invocation, analysis, output = self.build_output_fixture()
                try:
                    events = self.events(invocation)
                    leaf = events[-1]["namedSetOfFiles"]
                    if damage == "missing-file":
                        output.unlink()
                    elif damage == "digest":
                        output.write_text("changed bytes\n")
                    elif damage in ("missing-group", "failed-build"):
                        next(event["completed"] for event in events if "completed" in event)["outputGroup"] = []
                        if damage == "failed-build":
                            (invocation / "native-exit").write_text("1\n")
                    elif damage == "missing-set":
                        events.pop()
                    elif damage == "cycle":
                        leaf["fileSets"] = [{"id": "parent"}]
                    elif damage == "empty":
                        leaf["files"] = []
                    self.write_events(invocation, events)
                    captured = report.capture(invocation / "events.jsonl", invocation / "captured.jsonl",
                                              invocation, invocation / "reports", expected_analysis=(analysis,))
                    if damage == "changed-copy":
                        (invocation / captured["build_outputs"][0]["path"]).write_text("damaged retained report")
                    value = self.verdict(invocation, analysis=analysis)
                    self.assertEqual(value["status"], expected_status, value)
                    if error is not None:
                        self.assertTrue(any(error in item for item in value["errors"]), value)
                    else:
                        self.assertEqual(len(value["build_outputs"]), 1)
                        preflight = report.preflight(analysis, 0, "build")
                        self.assertEqual(preflight["output_options"], ["--output_groups=+default", "--remote_download_outputs=toplevel"])
                        # Resume can authenticate the earlier retained copy even
                        # when the original output is no longer available.
                        output.unlink()
                        resumed = report.capture(invocation / "events.jsonl", invocation / "captured.jsonl",
                                                invocation, invocation / "reports", expected_analysis=(analysis,))
                        self.assertEqual(resumed["errors"], [])
                    archive = self.root / "build-outputs.tar.gz"
                    combined = report.bundle(self.evidence, int(damage == "failed-build"), 0, archive)
                    self.assertEqual(combined["status"], expected_status, combined)
                    receipt = report.receipt(archive, report.file_record(archive)["sha256"], value["source"])
                    self.assertEqual(receipt["status"], expected_status, receipt)
                finally:
                    shutil.rmtree(invocation)

    def test_command_completion_preserves_streams_and_failure_evidence(self) -> None:
        for command, exit_code, damage, status in (
            ("query", 0, None, "PASS"),
            ("run", 0, "empty", "PASS"),
            ("run", 23, None, "FAIL"),
            ("query", 23, "stderr", "FAIL"),
            ("query", 0, "stdout", "INCOMPLETE"),
            ("query", 0, "native-exit", "INCOMPLETE"),
            ("query", 0, "arguments", "INCOMPLETE"),
            ("query", 0, "source-after", "INCOMPLETE"),
            ("run", 23, "finish", "FAIL"),
            ("run", 0, "finish", "INCOMPLETE"),
            ("run", 23, "capture-exit", "FAIL"),
            ("run", 0, "analysis", "INCOMPLETE"),
            ("run", 0, "tool-argument", "PASS"),
        ):
            with self.subTest(command=command, exit_code=exit_code, damage=damage):
                invocation = self.fixture()
                try:
                    context = report.read_json(invocation / "context.json")
                    arguments = [command, "--literal=spaces # and\nnewlines", "//:owner"]
                    if damage == "analysis":
                        arguments.insert(1, "--nobuild")
                    elif damage == "tool-argument":
                        arguments.extend(["--", "--nobuild"])
                    context.update(command_kind="command", executed_arguments="arguments.json")
                    if damage == "finish":
                        context["execution_finished_at"] = None
                    report.write_json(invocation / "context.json", context)
                    report.write_json(invocation / "arguments.json", arguments)
                    (invocation / context["native_exit"]).write_text(str(exit_code))
                    (invocation / "stdout.log").write_bytes(b"<query>\x00\nraw</query>")
                    (invocation / "stderr.log").write_bytes(b"diagnostic without final newline")
                    if damage == "empty":
                        (invocation / "stdout.log").write_bytes(b"")
                        (invocation / "stderr.log").write_bytes(b"")
                    elif damage == "arguments":
                        report.write_json(invocation / "arguments.json", ["query", 7])
                    elif damage == "source-after":
                        (invocation / "source-after.txt").write_text(" M changed.go\n")
                    elif damage == "native-exit":
                        (invocation / context["native_exit"]).unlink()
                    elif damage == "capture-exit":
                        (invocation / context["capture_exit"]).unlink()
                    elif damage in ("stdout", "stderr"):
                        (invocation / (damage + ".log")).unlink()
                    value = report.command_verdict(invocation / "context.json")
                    report.write_json(invocation / "verdict.json", value)
                    self.assertEqual(value["status"], status, value)
                    if damage != "arguments":
                        self.assertEqual(value["arguments"], arguments)
                    archive = self.root / "commands.tar.gz"
                    combined = report.bundle(self.evidence, exit_code, 0, archive)
                    self.assertEqual(combined["status"], status, combined)
                    receipt = report.receipt(archive, report.file_record(archive)["sha256"], context["expected_source"])
                    self.assertEqual(receipt["status"], status, receipt)
                    self.assertEqual(receipt["expected_owner_count"], 0)
                    self.assertEqual(receipt["executed_attempt_count"], 0)
                    self.assertEqual(receipt["invocations"][0]["expected_owner_count"], 0)
                    self.assertEqual(receipt["invocations"][0]["executed_attempt_count"], 0)
                    self.assertEqual(receipt["case_counts"], {})
                    [record] = receipt["commands"]
                    self.assertEqual(record["bazel_command"], None if damage == "arguments" else command)
                    with tarfile.open(archive) as bundle:
                        for name, identity in record["command_outputs"].items():
                            retained = bundle.extractfile("invocation-pass/" + identity["path"]).read()
                            self.assertEqual(retained, (invocation / (name + ".log")).read_bytes())
                            self.assertEqual(hashlib.sha256(retained).hexdigest(), identity["sha256"])
                finally:
                    shutil.rmtree(invocation)

    def test_missing_finish_time_preserves_retained_test_failure(self) -> None:
        invocation = self.fixture("fail")
        self.capture(invocation)
        context = report.read_json(invocation / "context.json")
        context["execution_finished_at"] = "damaged timestamp"
        report.write_json(invocation / "context.json", context)
        value = self.verdict(invocation)
        self.assertEqual(value["status"], "FAIL", value)
        self.assertIsNone(value["execution_finished_at"])
        self.assertIsNone(value["execution_to_verdict_seconds"])
        self.assertTrue(any("execution_finished_at" in error for error in value["errors"]))

    @staticmethod
    def jtreg_output(name: str, result: str | None, stdout: str = "") -> str:
        output = ("--------------------------------------------------\n"
                  f"TEST: {name}\nTEST JDK: /usr/lib/jvm/java-21-openjdk-amd64\n"
                  f"\nACTION: main -- Passed.\nSTDOUT:\n{stdout}\n")
        if result is not None:
            output += f"TEST RESULT: {result}\n--------------------------------------------------\n"
        return output

    def runtime_fixture(self, output: str, *, zipped: bool, names: str = "First.java,Second.java",
                        tag: str = "jtreg-results") -> Path:
        invocation = self.fixture()
        events = self.events(invocation)
        configured = next(event["configured"] for event in events if "configured" in event)
        configured["tag"].append(tag)
        (invocation / "test.log").write_text(
            f"I1018 00:00:00.000000 1 lib.go:243] Running test case batch: {names}\n"
            "PASS: (1s) 2 tests passed\n")
        batch = "Batch:\n" + names.replace(",", "\n") + "\nOutput:\n" + output
        result = next(event["testResult"] for event in events if "testResult" in event)
        if zipped:
            path = invocation / "outputs.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("batch-0.log", batch)
            name = "test.outputs__outputs.zip"
        else:
            path = invocation / "batch-0.log"
            path.write_text(batch)
            name = "test.outputs/batch-0.log"
        result["testActionOutput"].append({"name": name, "uri": path.as_uri()})
        self.write_events(invocation, events)
        return invocation

    @staticmethod
    def php_native_files(names: list[str], rows: list[tuple[str, str]], *, redirect: bool = False) -> dict[str, str]:
        """A source-shaped -W/JUnit pair, including PHP's redirect remapping."""
        cases = []
        counts = {"failures": 0, "errors": 0, "skip": 0}
        for state, name in rows:
            if name.startswith("# "):
                parent, child = name[2:].split(": ")
                xml_name = str(Path(parent).parent / Path(child).name)
            else:
                xml_name = name
            tag = {"FAILED": "failure", "BORKED": "error", "LEAKED": "error",
                   "WARNED": "warning", "SKIPPED": "skipped"}.get(state)
            if tag:
                attribute = {"failure": "failures", "error": "errors", "skipped": "skip"}.get(tag)
                if attribute:
                    counts[attribute] += 1
            cases.append(f'<testcase name="{xml_name} (description)">' +
                         (f"<{tag}>native reason</{tag}>" if tag else "") + "</testcase>")
        if any(name.startswith("# ") for _, name in rows):
            cases.append(f'<testcase name="{names[0]} (redirect parent)"/>')
        attributes = f'tests="{len(cases)}" ' + " ".join(f'{key}="{value}"' for key, value in counts.items())
        native = "".join(state + "\t" + ("# /php/" + name[2:].replace(": ", ": /php/")
                                         if name.startswith("# ") else "/php/" + name) + "\n"
                         for state, name in rows)
        return {"selection.txt": "".join(name + "\n" for name in names), "root.txt": "/php\n",
                "redirect.txt": "true\n" if redirect else "false\n", "results.tsv": native,
                "junit.xml": f'<testsuites name="php" {attributes}><testsuite name="php.ext.pdo.sqlite.tests" {attributes}>' +
                             "".join(cases) + "</testsuite></testsuites>"}

    def php_fixture(self, groups: dict[str, dict[str, str]], *, zipped: bool = True,
                    names: str = "first.phpt,second.phpt", markers: list[str] | None = None) -> Path:
        output = "".join("gvisor-php-report /proctor-artifacts/" + group + "\n"
                         for group in (markers if markers is not None else groups))
        invocation = self.runtime_fixture(output, zipped=zipped, names=names, tag="php-results")
        if zipped:
            with zipfile.ZipFile(invocation / "outputs.zip", "a") as archive:
                for group, files in groups.items():
                    for name, content in files.items():
                        archive.writestr(group + "/" + name, content)
        else:
            events = self.events(invocation)
            result = next(event["testResult"] for event in events if "testResult" in event)
            for group, files in groups.items():
                (invocation / group).mkdir()
                for name, content in files.items():
                    path = invocation / group / name
                    path.write_text(content)
                    result["testActionOutput"].append({"name": "test.outputs/" + group + "/" + name,
                                                      "uri": path.as_uri()})
            self.write_events(invocation, events)
        return invocation

    def test_php_native_states_and_retention(self) -> None:
        for zipped, state in itertools.product((False, True), ("PASSED", "SKIPPED", "XFAILED", "XLEAKED",
                                                              "FAILED", "WARNED", "LEAKED", "BORKED")):
            with self.subTest(zipped=zipped, state=state):
                files = self.php_native_files(["first.phpt", "second.phpt"],
                                             [("PASSED", "first.phpt"), (state, "second.phpt")])
                invocation = self.php_fixture({"php-results.first": files}, zipped=zipped)
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], "FAIL" if state in ("FAILED", "WARNED", "LEAKED", "BORKED") else "PASS", value)
                native = value["executed"][0]["php"]
                self.assertEqual(native["results"]["php-results.first"]["second.phpt"], state)
                self.assertEqual(native["passed"], 2 if state == "PASSED" else 1)
                if state == "SKIPPED":
                    self.assertEqual(native["skipped"], {"php-results.first/second.phpt": "native reason"})
                if state in ("XFAILED", "XLEAKED"):
                    self.assertEqual(native["expected_failures"], {"php-results.first/second.phpt": state})
                shutil.rmtree(invocation)

    def test_php_incomplete_native_reports_cannot_pass(self) -> None:
        mutations = {
            "missing XML": lambda files: files.pop("junit.xml"),
            "partial XML": lambda files: files.update({"junit.xml": files["junit.xml"][:-12]}),
            "whole-row truncation": lambda files: files.update({"results.tsv": "PASSED\t/php/first.phpt\n"}),
            "partial-row truncation": lambda files: files.update({"results.tsv": files["results.tsv"].rstrip()}),
            "duplicate row": lambda files: files.update({"results.tsv": files["results.tsv"] * 2}),
            "native counter mismatch": lambda files: files.update({"junit.xml": files["junit.xml"].replace('tests="2"', 'tests="3"')}),
            "foreign result": lambda files: files.update({"results.tsv": files["results.tsv"].replace("second.phpt", "other.phpt")}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                files = self.php_native_files(["first.phpt", "second.phpt"],
                                             [("PASSED", "first.phpt"), ("PASSED", "second.phpt")])
                mutate(files)
                invocation = self.php_fixture({"php-results.first": files})
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], "INCOMPLETE", value)
                shutil.rmtree(invocation)

    def test_php_failure_survives_later_incomplete_report(self) -> None:
        files = self.php_native_files(["first.phpt", "second.phpt"],
                                     [("FAILED", "first.phpt"), ("PASSED", "second.phpt")])
        files["results.tsv"] += "invalid row\n"
        invocation = self.php_fixture({"php-results.first": files})
        self.assertEqual(self.capture(invocation)["errors"], [])
        self.assertEqual(self.verdict(invocation, analysis=self.analysis(invocation))["status"], "FAIL")

    def test_php_redirect_completion_keeps_context(self) -> None:
        parent, child = "ext/pdo_sqlite/tests/common.phpt", "ext/pdo/tests/first.phpt"
        for timed_parent in (False, True):
            with self.subTest(timed_parent=timed_parent):
                files = self.php_native_files([parent], [("PASSED", "# " + parent + ": " + child)], redirect=True)
                if not timed_parent:
                    # No SKIPIF timer means the completed parent increments
                    # counters but has no entry in PHP's XML file registry.
                    files["junit.xml"] = files["junit.xml"].replace(
                        f'<testcase name="{parent} (redirect parent)"/>', "")
                invocation = self.php_fixture({"php-results.redirect": files}, names=parent)
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], "PASS", value)
                self.assertEqual(value["executed"][0]["php"]["redirects"],
                                 {"php-results.redirect/" + parent: ["# " + parent + ": " + child]})
                shutil.rmtree(invocation)
        # A parent result alone cannot prove that its expansion completed.
        files["results.tsv"] = ""
        invocation = self.php_fixture({"php-results.redirect": files}, names=parent)
        self.assertEqual(self.capture(invocation)["errors"], [])
        self.assertEqual(self.verdict(invocation, analysis=self.analysis(invocation))["status"], "INCOMPLETE")

    def test_php_redirect_rejects_incomplete_completion_and_colliding_children(self) -> None:
        parent = "ext/pdo_sqlite/tests/common.phpt"
        rows = [("PASSED", "# " + parent + ": ext/pdo/tests/first.phpt")]
        for mutation in ("missing completion", "missing child", "wrong suite", "colliding children"):
            with self.subTest(mutation=mutation):
                files = self.php_native_files([parent], rows, redirect=True)
                files["junit.xml"] = files["junit.xml"].replace(
                    f'<testcase name="{parent} (redirect parent)"/>', "")
                if mutation == "missing completion":
                    files["junit.xml"] = files["junit.xml"].replace('tests="2"', 'tests="1"')
                elif mutation == "missing child":
                    files["junit.xml"] = files["junit.xml"].replace(
                        '<testcase name="ext/pdo_sqlite/tests/first.phpt (description)"></testcase>', "")
                elif mutation == "wrong suite":
                    files["junit.xml"] = files["junit.xml"].replace('name="php.ext.pdo.sqlite.tests"', 'name="php.other"')
                else:
                    files["results.tsv"] += "PASSED\t# /php/" + parent + ": /php/other/first.phpt\n"
                invocation = self.php_fixture({"php-results.redirect": files}, names=parent)
                self.assertEqual(self.capture(invocation)["errors"], [])
                self.assertEqual(self.verdict(invocation, analysis=self.analysis(invocation))["status"], "INCOMPLETE")
                shutil.rmtree(invocation)

    def test_php_redirect_early_skip_keeps_original_filename(self) -> None:
        parent, child = "ext/pdo_sqlite/tests/common.phpt", "ext/pdo/tests/first.phpt"
        files = self.php_native_files([parent], [("SKIPPED", "# " + parent + ": " + child)], redirect=True)
        files["junit.xml"] = files["junit.xml"].replace("ext/pdo_sqlite/tests/first.phpt", child)
        invocation = self.php_fixture({"php-results.redirect": files}, names=parent)
        self.assertEqual(self.capture(invocation)["errors"], [])
        value = self.verdict(invocation, analysis=self.analysis(invocation))
        self.assertEqual(value["status"], "PASS", value)
        self.assertEqual(value["executed"][0]["php"]["skipped"],
                         {"php-results.redirect/# " + parent + ": " + child: "native reason"})

        shutil.rmtree(invocation)
        # Missing EXTENSIONS can skip after redirect remapping but before its
        # timer. Both the child SKIP and parent completion then use counters.
        files = self.php_native_files([parent], [("SKIPPED", "# " + parent + ": " + child)], redirect=True)
        files["junit.xml"] = files["junit.xml"].replace(
            '<testcase name="ext/pdo_sqlite/tests/first.phpt (description)"><skipped>native reason</skipped></testcase>', "").replace(
            f'<testcase name="{parent} (redirect parent)"/>', "")
        invocation = self.php_fixture({"php-results.redirect": files}, names=parent)
        self.assertEqual(self.capture(invocation)["errors"], [])
        value = self.verdict(invocation, analysis=self.analysis(invocation))
        self.assertEqual(value["status"], "PASS", value)
        self.assertEqual(value["executed"][0]["php"]["counter_only"], {
            "php-results.redirect/" + parent: "REDIR",
            "php-results.redirect/# " + parent + ": " + child: "SKIPPED",
        })

    def test_php_counter_only_early_skip(self) -> None:
        name = "ext/ffi/tests/001.phpt"
        for mutation, status in ((None, "PASS"), ("wrong suite", "INCOMPLETE"),
                                 ("missing skip count", "INCOMPLETE"), ("missing pass", "INCOMPLETE")):
            with self.subTest(mutation=mutation):
                files = self.php_native_files([name], [("SKIPPED", name)])
                files["junit.xml"] = files["junit.xml"].replace(
                    'name="php.ext.pdo.sqlite.tests"', 'name="php.ext.ffi.tests"').replace(
                    f'<testcase name="{name} (description)"><skipped>native reason</skipped></testcase>', "")
                if mutation == "wrong suite":
                    files["junit.xml"] = files["junit.xml"].replace('name="php.ext.ffi.tests"', 'name="php.other"')
                elif mutation == "missing skip count":
                    files["junit.xml"] = files["junit.xml"].replace('skip="1"', 'skip="0"')
                elif mutation == "missing pass":
                    files["results.tsv"] = files["results.tsv"].replace("SKIPPED", "PASSED")
                invocation = self.php_fixture({"php-results.early": files}, names=name)
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], status, value)
                if status == "PASS":
                    native = value["executed"][0]["php"]
                    self.assertEqual(native["counter_only"], {"php-results.early/" + name: "SKIPPED"})
                    self.assertEqual(native["skipped"], {"php-results.early/" + name: ""})
                shutil.rmtree(invocation)

    def test_php_counter_only_suite_depth(self) -> None:
        # Pinned PHP normalizes shallow suite names, but its five-ancestor
        # search falls back to the relative path for deeper directories.
        for name, suite in (
                ("ext/pdo_sqlite/tests/common.phpt", "php.ext.pdo.sqlite.tests"),
                ("ext/standard/tests/file/windows_mb_path/test_long_path_0.phpt",
                 "php.ext.standard.tests.file.windows_mb_path")):
            with self.subTest(name=name):
                files = self.php_native_files([name], [("SKIPPED", name)])
                files["junit.xml"] = files["junit.xml"].replace(
                    'name="php.ext.pdo.sqlite.tests"', f'name="{suite}"').replace(
                    f'<testcase name="{name} (description)"><skipped>native reason</skipped></testcase>', "")
                invocation = self.php_fixture({"php-results.depth": files}, names=name)
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], "PASS", value)
                self.assertEqual(value["executed"][0]["php"]["counter_only"],
                                 {"php-results.depth/" + name: "SKIPPED"})
                shutil.rmtree(invocation)

    def test_php_unique_execution_groups_and_repetition(self) -> None:
        groups = {f"php-results.{number}": self.php_native_files([name], [("PASSED", name)])
                  for number, name in enumerate(["first.phpt", "first.phpt", "second.phpt", "second.phpt"])}
        for markers, expected in ((None, "PASS"), (list(groups)[:-1], "INCOMPLETE"),
                                  ([*groups, "php-results.0"], "INCOMPLETE")):
            with self.subTest(markers=markers):
                invocation = self.php_fixture(groups, markers=markers)
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], expected, value)
                shutil.rmtree(invocation)

    def test_jtreg_results_override_successful_wrapper(self) -> None:
        scenarios = (
            (self.jtreg_output("First.java", "Passed.") + self.jtreg_output("Second.java", "Passed."), "PASS"),
            (self.jtreg_output("First.java", "Passed.") + self.jtreg_output("Second.java", "Failed. timeout"), "FAIL"),
            (self.jtreg_output("First.java", "Passed."), "INCOMPLETE"),
            (self.jtreg_output("First.java", "Passed.") + self.jtreg_output("Second.java", None), "INCOMPLETE"),
            ("", "INCOMPLETE"),
            (self.jtreg_output("First.java", "Passed.") + self.jtreg_output("First.java", "Passed."), "INCOMPLETE"),
            (self.jtreg_output("Unselected.java", "Passed."), "INCOMPLETE"),
            (self.jtreg_output("First.java", "Failed.") + self.jtreg_output("First.java", "Passed."), "FAIL"),
            (self.jtreg_output("First.java", "Error.") + self.jtreg_output("Unselected.java", "Passed."), "FAIL"),
            ("TEST: First.java\nTEST RESULT: Passed.\n", "INCOMPLETE"),
            (self.jtreg_output("First.java", "Passed.").removesuffix("--------------------------------------------------\n"), "INCOMPLETE"),
        )
        for zipped, (output, expected) in itertools.product((False, True), scenarios):
            with self.subTest(zipped=zipped, output=output):
                invocation = self.runtime_fixture(output, zipped=zipped)
                # A result can precede its configured-parent event in BEP.
                events = self.events(invocation)
                result = next(event["testResult"] for event in events if "testResult" in event)
                result["testActionOutput"].append({
                    "name": "test.outputs/boot.log", "uri": (invocation / "unmaterialized-debug.log").as_uri()})
                diagnostic = invocation / "batch-0-sentry-stacks.log"
                diagnostic.write_text("goroutine 1 [running]:\n")
                result["testActionOutput"].append({
                    "name": "test.outputs/" + diagnostic.name, "uri": diagnostic.as_uri()})
                self.write_events(invocation, events[1:] + events[:1])
                # The configured contract retains passing artifacts without
                # relying on the caller to request all undeclared outputs.
                captured = self.capture(invocation)
                self.assertEqual(captured["errors"], [])
                self.assertIn("test.outputs/" + diagnostic.name, [item["name"] for item in captured["reports"]])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], expected, value)
                if expected == "PASS":
                    self.assertEqual(value["executed"][0]["jtreg"]["passed"], 2)
                shutil.rmtree(invocation)

    def test_node_completion_overrides_successful_wrapper(self) -> None:
        # This is the pinned v22.2 tools/test.py TapProgressIndicator contract,
        # not an execution transcript. In particular, an IOError can leave
        # fewer HasRun frames than Starting's plan despite a zero process exit.
        first = "ok 1 parallel/first\n  ---\n  duration_ms: 1.00000\n  ...\n"
        second = "ok 2 parallel/second\n  ---\n  duration_ms: 2.00000\n  ...\n"
        failed = second.replace("ok 2", "not ok 2")
        skipped = second.replace("parallel/second", "parallel/second # skip unavailable")
        todo = second.replace("parallel/second", "parallel/second # TODO : Fix flaky test")
        plan = "TAP version 13\n1..2\n"
        heartbeat = "2026/10/19 23:42:26 INFO Proctor checking in 2026-10-19 23:42:26 +0000 UTC\n"
        scenarios = (
            (plan + first + second, "PASS", 2),
            (plan + first + second + "\nAll tests passed.\n", "PASS", 2),
            (plan + first + heartbeat + second, "PASS", 2),
            (plan + first + failed, "FAIL", 1),
            (plan + first + failed + "\nFailed tests:\n/root/node/out/Release/node parallel/second.js\n", "FAIL", 1),
            (plan + first + second + "\nFailed tests:\n", "INCOMPLETE", 2),
            (plan + first + "\nAll tests passed.\n", "INCOMPLETE", 1),
            (plan + first + "\nAll tests passed.\n" + second, "INCOMPLETE", 1),
            (plan + first + second + "\nAll tests passed.\n" + plan, "INCOMPLETE", 2),
            (plan + first + failed + "\nFailed tests:\n" + second, "FAIL", 1),
            (plan + first, "INCOMPLETE", 1),
            (plan, "INCOMPLETE", 0),
            ("", "INCOMPLETE", 0),
            ("[00:01|% 100|+   2|-   0]: Done\n", "INCOMPLETE", 0),
            (plan + first + second.removesuffix("  ...\n"), "INCOMPLETE", 1),
            (plan + first + failed.removesuffix("  ...\n"), "FAIL", 1),
            (plan + first + second.replace("ok 2", "ok 1"), "INCOMPLETE", 2),
            (plan + first + second.replace("parallel/second", "parallel/first"), "INCOMPLETE", 1),
            (plan + first + second.replace("parallel/second", "parallel/unknown"), "INCOMPLETE", 1),
            (plan + first + second + plan, "INCOMPLETE", 2),
            (plan.replace("1..2", "1..0") + first + second, "INCOMPLETE", 2),
            (plan + first + skipped, "PASS", 1),
            (plan + first + todo, "PASS", 2),
            (plan + first + todo.replace("ok 2", "not ok 2"), "FAIL", 1),
            (plan + first.replace("parallel/first", "parallel/first # TODO : Fix flaky test") + todo, "PASS", 2),
            (plan + first.replace("parallel/first", "parallel/first # skip unavailable") + skipped, "INCOMPLETE", 0),
        )
        for zipped, (output, expected, passed) in itertools.product((False, True), scenarios):
            with self.subTest(zipped=zipped, output=output):
                invocation = self.runtime_fixture(
                    output, zipped=zipped, names="parallel/first.js,parallel/second.js", tag="nodejs-results")
                captured = self.capture(invocation)
                self.assertEqual(captured["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], expected, value)
                runtime = value["executed"][0]["nodejs"]
                self.assertEqual(runtime["passed"], passed)
                self.assertEqual(set(runtime["skipped"]), {
                    "parallel/" + name + ".js" for name in ("first", "second")
                    if f"parallel/{name} # skip" in output
                })
                self.assertEqual(set(runtime["expected_flaky"]), {
                    "parallel/" + name + ".js" for name in ("first", "second")
                    if f"parallel/{name} # TODO" in output
                })
                shutil.rmtree(invocation)

    def test_node_exact_case_identity_contract(self) -> None:
        # Synthetic source-patch output exercises actual capture/tag dispatch.
        # Filename extensions and encoded WPT variants are separate cases; a
        # legacy display name or another variant cannot satisfy the selection.
        names = ["parallel/test-worker.js", "parallel/test-worker.mjs",
                 "wpt/test-fetch.js/fetch/basic.any.html%3Fq%3Da%2Cb",
                 "wpt/test-fetch.js/fetch/basic.any.html%3Fq%3Da%252Cb"]
        marker = "# gvisor-node-exact-ids\n"
        frames = [f"ok {index} {name}\n  ---\n  duration_ms: 1.00000\n  ...\n"
                  for index, name in enumerate(names, 1)]
        output = marker + "TAP version 13\n1..4\n" + "".join(frames) + "All tests passed.\n"
        exclusion = '# gvisor-node-skip ' + json.dumps({"name": names[-1], "reason": "status-file SKIP"}) + "\n"
        scenarios = (
            (output, "PASS", 4, set()),
            (output.removeprefix(marker), "INCOMPLETE", 4, set()),
            (marker + output, "INCOMPLETE", 4, set()),
            (output.replace(names[1], names[0]), "INCOMPLETE", 3, set()),
            (output.replace(names[0], "parallel/test-worker"), "INCOMPLETE", 3, set()),
            (output.replace(names[-1], names[-2]), "INCOMPLETE", 3, set()),
            (output.replace(names[-1], names[-1] + "%20unknown"), "INCOMPLETE", 3, set()),
            (output.replace(frames[-1], ""), "INCOMPLETE", 3, set()),
            (output.replace("ok 4", "not ok 4").replace("All tests passed.", "Failed tests:"), "FAIL", 3, set()),
            (output.replace(marker, marker + exclusion).replace("1..4", "1..3").replace(frames[-1], ""),
             "PASS", 3, {names[-1]}),
            (output.replace(marker, exclusion + marker).replace("1..4", "1..3").replace(frames[-1], ""),
             "INCOMPLETE", 3, set()),
        )
        for zipped, (body, status, passed, excluded) in itertools.product((False, True), scenarios):
            with self.subTest(zipped=zipped, body=body):
                invocation = self.runtime_fixture(
                    body, zipped=zipped, names=",".join(names), tag="nodejs-exact-results")
                captured = self.capture(invocation)
                self.assertEqual(captured["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], status, value)
                runtime = value["executed"][0]["nodejs"]
                self.assertEqual(runtime["exact_ids"], True)
                self.assertEqual(runtime["passed"], passed)
                self.assertEqual(set(runtime["excluded"]), excluded)
                shutil.rmtree(invocation)

    def test_node_classifier_exclusions_are_not_completed_results(self) -> None:
        # Synthetic image-patch records expose should_keep decisions without
        # duplicating Node's platform/mode/status-file classifier.
        first = '# gvisor-node-skip {"name": "parallel/first", "reason": "status-file SKIP"}\n'
        second = '# gvisor-node-skip {"name": "parallel/second", "reason": "status-file SKIP"}\n'
        result = "TAP version 13\n1..1\nok 1 parallel/first\n  ---\n  duration_ms: 1.00000\n  ...\n"
        scenarios = (
            (second + result + "All tests passed.\n", "PASS", 1, {"parallel/second.js"}),
            (first + second + "No tests to run.\n", "INCOMPLETE", 0, {"parallel/first.js", "parallel/second.js"}),
            (result + "All tests passed.\n", "INCOMPLETE", 1, set()),
            (second + second + result, "INCOMPLETE", 1, {"parallel/second.js"}),
            (first + result, "INCOMPLETE", 0, {"parallel/first.js"}),
            (result + second, "INCOMPLETE", 1, set()),
            (second.replace("parallel/second", "parallel/unknown") + result, "INCOMPLETE", 1, set()),
            (second.replace("status-file SKIP", "unknown") + result, "INCOMPLETE", 1, set()),
            ('# gvisor-node-skip {"name":\n' + result, "INCOMPLETE", 1, set()),
            (second + result.replace("1..1", "1..2"), "INCOMPLETE", 1, {"parallel/second.js"}),
        )
        for output, expected, passed, excluded in scenarios:
            with self.subTest(output=output):
                invocation = self.runtime_fixture(
                    output, zipped=False, names="parallel/first.js,parallel/second.js", tag="nodejs-results")
                self.capture(invocation)
                value = self.verdict(invocation)
                self.assertEqual(value["status"], expected, value)
                runtime = value["executed"][0]["nodejs"]
                self.assertEqual(runtime["passed"], passed)
                self.assertEqual(set(runtime["excluded"]), excluded)
                self.assertEqual(set(runtime["skipped"]), excluded)
                shutil.rmtree(invocation)

    def test_node_child_output_cannot_complete_missing_test(self) -> None:
        # The emitter indents diagnostic stdout/stderr. A failed child may
        # print TAP-looking output, but only the outer HasRun frame counts.
        output = ("TAP version 13\n1..2\nnot ok 1 parallel/first\n"
                  "  ---\n  duration_ms: 1.00000\n  severity: fail\n  stack: |-\n"
                  "    ok 2 parallel/second\n    1..2\n    TAP version 13\n"
                  "      ---\n      duration_ms: 2.00000\n      ...\n  ...\n")
        invocation = self.runtime_fixture(
            output, zipped=False, names="parallel/first.js,parallel/second.js", tag="nodejs-results")
        self.capture(invocation)
        value = self.verdict(invocation)
        self.assertEqual(value["status"], "FAIL", value)
        self.assertEqual(value["executed"][0]["nodejs"]["results"], {"parallel/first.js": "not ok"})
        self.assertEqual(value["executed"][0]["nodejs"]["missing"], ["parallel/second.js"])

    def test_retained_node_tap_batches(self) -> None:
        # These are unchanged authenticated outputs, including Main's footer.
        # Batch 0 retains the selected fixture that Node never enumerated.
        scenarios = (
            ("batch-0.log", "INCOMPLETE", 44, 5,
             ["fixtures/test-runner/default-behavior/node_modules/test-nm.js"]),
            ("batch-150.log", "PASS", 50, 0, []),
        )
        for filename, status, passed, skipped, missing in scenarios:
            with self.subTest(filename=filename):
                selection, separator, body = (FIXTURES / "node-tap" / filename).read_text().partition("\nOutput:\n")
                self.assertEqual(separator, "\nOutput:\n")
                names = selection.removeprefix("Batch:\n").splitlines()
                invocation = self.runtime_fixture(
                    body, zipped=False, names=",".join(names), tag="nodejs-results")
                self.capture(invocation)
                value = self.verdict(invocation)
                self.assertEqual(value["status"], status, value)
                runtime = value["executed"][0]["nodejs"]
                self.assertEqual(runtime["passed"], passed)
                self.assertEqual(len(runtime["skipped"]), skipped)
                self.assertEqual(runtime["missing"], missing)
                self.assertEqual(runtime["excluded"], {})
                if status == "PASS":
                    self.assertEqual(runtime["results"]["parallel/test-http-outgoing-end-cork.js"], "ok")
                shutil.rmtree(invocation)

    def test_jtreg_section_stdout_is_not_result_framing(self) -> None:
        # Actual section output from DefaultProvider.java and the RMI tests
        # contains TEST: lines. A result-like stdout line is also ordinary text.
        stdout = ("TEST: Default provider with constructor\n"
                  "TEST: SHA1PRNG supported on all platforms by SUN provider\n"
                  "--------------------------------------------------\n"
                  "TEST: rmi\nTEST RESULT: Failed. printed by the test\n")
        for zipped in (False, True):
            with self.subTest(zipped=zipped):
                invocation = self.runtime_fixture(
                    self.jtreg_output("First.java", "Passed.", stdout)
                    + self.jtreg_output("Second.java", "Passed."), zipped=zipped)
                self.capture(invocation)
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], "PASS", value)
                self.assertEqual(value["executed"][0]["jtreg"]["results"],
                                 {"First.java": "Passed.", "Second.java": "Passed."})
                shutil.rmtree(invocation)

    def test_jtreg_heartbeat_between_shared_separator_and_header(self) -> None:
        heartbeat = ("2026/10/19 18:18:12 INFO Proctor checking in "
                     "2026-10-19 18:18:12.745228539 +0000 UTC m=+15.002775386\n")
        output = (self.jtreg_output("First.java", "Passed.") + heartbeat
                  + self.jtreg_output("Second.java", "Passed.")
                  .removeprefix("--------------------------------------------------\n"))
        invocation = self.runtime_fixture(output, zipped=False)
        self.capture(invocation)
        value = self.verdict(invocation, analysis=self.analysis(invocation))
        self.assertEqual(value["status"], "PASS", value)
        self.assertEqual(value["executed"][0]["jtreg"]["results"],
                         {"First.java": "Passed.", "Second.java": "Passed."})

    def test_jtreg_skipped_result_is_not_a_passed_body(self) -> None:
        result = "Passed. Skipped: jtreg.SkippedException: Could not find NSS"
        invocation = self.runtime_fixture(self.jtreg_output("First.java", result)
                                        + self.jtreg_output("Second.java", "Passed."), zipped=False)
        self.capture(invocation)
        value = self.verdict(invocation, analysis=self.analysis(invocation))
        self.assertEqual(value["status"], "PASS", value)
        jtreg = value["executed"][0]["jtreg"]
        self.assertEqual(jtreg["passed"], 1)
        self.assertEqual(jtreg["skipped"], {"First.java": result})
        self.assertTrue(any(item["name"] == "First.java" and item["reason"] == result for item in value["skips"]))

    def test_jtreg_requires_matching_retained_batches(self) -> None:
        for missing in (True, False):
            with self.subTest(missing=missing):
                invocation = self.runtime_fixture(self.jtreg_output("First.java", "Passed."), zipped=False)
                events = self.events(invocation)
                result = next(event["testResult"] for event in events if "testResult" in event)
                if missing:
                    result["testActionOutput"].pop()
                    self.write_events(invocation, events)
                else:
                    path = invocation / "batch-0.log"
                    path.write_text(path.read_text().replace("Second.java", "Third.java"))
                self.capture(invocation)
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], "INCOMPLETE", value)
                self.assertTrue(any("batches differ" in error for error in value["errors"]), value)
                shutil.rmtree(invocation)

    def test_jtreg_and_xml_failures_survive_other_report_errors(self) -> None:
        for malformed_xml in (False, True):
            with self.subTest(malformed_xml=malformed_xml):
                invocation = self.runtime_fixture(self.jtreg_output("First.java", "Failed."), zipped=False)
                if malformed_xml:
                    (invocation / "test.xml").write_text("<truncated")
                else:
                    (invocation / "test.log").write_text("No valid batch selection\n")
                    (invocation / "test.xml").write_text(
                        '<testsuite tests="1" failures="1"><testcase name="batch_0"><failure/></testcase></testsuite>')
                self.capture(invocation)
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], "FAIL", value)
                self.assertTrue(value["errors"], value)
                self.assertTrue(value["failures"], value)
                shutil.rmtree(invocation)

    def benchmark_fixture(self) -> Path:
        invocation = self.fixture("benchmark")
        context = report.read_json(invocation / "context.json")
        context["command_kind"] = "benchmark"
        report.write_json(invocation / "context.json", context)
        arguments, _ = report.benchmark_manifest(invocation / "benchmarks.json")
        with (invocation / "arguments.txt").open("a") as stream:
            stream.writelines("--test_arg=" + argument + "\n" for argument in arguments)
        # Model the future nonverbose invocation explicitly. Preserve the
        # original fixture XML and log so their false XML errors are testable.
        original = (invocation / "test.log").read_text()
        (invocation / "test.log").write_text("".join(line.replace("\x16", "") + "\n"
                                                     for line in original.splitlines() if not line.startswith("\x16===")))
        (invocation / "test.xml").write_text("<testsuites/>\n")
        return invocation

    def benchmark_verdict(self, invocation: Path) -> dict[str, object]:
        self.assertEqual(self.capture(invocation)["errors"], [])
        return self.verdict(invocation, analysis=invocation / "expected-analysis.jsonl",
                            benchmarks=invocation / "benchmarks.json")

    def pgo_fixture(self, zipped: bool = False, decision: str = "refresh") -> Path:
        invocation = self.benchmark_fixture()
        manifest = report.read_json(invocation / "benchmarks.json")
        (owner,) = report.mapping(manifest["owners"])
        manifest["arguments"] = ["-test.v=true", "-test.bench=.", "-test.benchtime=20s"]
        manifest["pgo"] = {owner: {"excluded": ["BenchmarkExcluded"], "platform": "systrap", "benchmark": "state"}}
        report.write_json(invocation / "benchmarks.json", manifest)
        arguments = (invocation / "arguments.txt").read_text().splitlines()
        (invocation / "arguments.txt").write_text("\n".join(
            [arg for arg in arguments if not arg.startswith("--test_arg=")]
            + ["--test_arg=" + argument for argument in manifest["arguments"]]) + "\n")
        # Go benchmark skips omit the duration printed for ordinary tests.
        log = (invocation / "test.log").read_text().replace("PASS\n", "--- SKIP: BenchmarkExcluded\nPASS\n")
        (invocation / "test.log").write_text(log)
        (invocation / "test.xml").write_text(
            '<testsuites><testsuite><testcase name="BenchmarkFirstRestore"/>'
            '<testcase name="BenchmarkFirstRestore/empty"/>'
            '<testcase name="BenchmarkExcluded"><skipped/></testcase></testsuite></testsuites>')
        profile = b"retained merged profile"
        metadata = {"runtime_key": "go1.27.1-linux-amd64", "platform": "systrap", "benchmark": "state",
                    "generated_sha256": hashlib.sha256(profile).hexdigest(), "cpu_samples": 123,
                    "decision": decision, "comparison": "" if decision == "add" else "real old/new comparison"}
        if decision != "add":
            metadata["baseline_sha256"] = "b" * 64
        outputs = {"pgo/result.json": json.dumps(metadata).encode(), "pgo/state.pgo.pprof.pb.gz": profile}
        events = self.events(invocation)
        result = next(event["testResult"] for event in events if "testResult" in event)
        if zipped:
            path = invocation / "outputs.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in outputs.items():
                    archive.writestr(name, data)
            result["testActionOutput"].append({"name": "test.outputs__outputs.zip", "uri": path.as_uri()})
        else:
            for name, data in outputs.items():
                path = invocation / "test.outputs" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                result["testActionOutput"].append({"name": "test.outputs/" + name, "uri": path.as_uri()})
        self.write_events(invocation, events)
        return invocation

    def pgo_verdict(self, invocation: Path) -> dict[str, object]:
        self.assertEqual(self.capture(invocation, retain_outputs=True)["errors"], [])
        return self.verdict(invocation, analysis=invocation / "expected-analysis.jsonl",
                            benchmarks=invocation / "benchmarks.json")

    def test_pgo_generation_is_distinct_from_refresh(self) -> None:
        for zipped, decision in itertools.product((False, True), ("add", "retain", "refresh")):
            with self.subTest(zipped=zipped, decision=decision):
                invocation = self.pgo_fixture(zipped, decision)
                value = self.pgo_verdict(invocation)
                self.assertEqual(value["status"], "PASS", value)
                profile = value["executed"][0]["pgo"]
                self.assertEqual((profile["generation"], profile["decision"]), ("PASS", decision))
                self.assertEqual(value["executed"][0]["pgo_excluded"], ["BenchmarkExcluded"])
                self.assertEqual(report.bundle(self.evidence, 0, 0, self.root / "bundle.tar.gz")["status"], "PASS")
                shutil.rmtree(invocation)

    def test_pgo_partial_zero_and_all_skipped_are_incomplete(self) -> None:
        for kind in ("partial", "zero", "all-skipped", "unexpected-skip", "missing-skip", "duplicate-skip"):
            with self.subTest(kind=kind):
                invocation = self.pgo_fixture()
                if kind == "partial":
                    manifest = report.read_json(invocation / "benchmarks.json")
                    (owner,) = report.mapping(manifest["owners"])
                    manifest["owners"][owner].append({"name": "BenchmarkMissing", "count": 1, "units": ["ns/op"]})
                    report.write_json(invocation / "benchmarks.json", manifest)
                elif kind in ("zero", "all-skipped"):
                    (invocation / "test.log").write_text("--- SKIP: BenchmarkExcluded\nPASS\n")
                    if kind == "all-skipped":
                        (invocation / "test.xml").write_text('<testsuites><testsuite><testcase name="BenchmarkExcluded"><skipped/></testcase></testsuite></testsuites>')
                else:
                    xml = invocation / "test.xml"
                    skipped = '<testcase name="BenchmarkExcluded"><skipped/></testcase>'
                    replacement = {"unexpected-skip": skipped.replace("BenchmarkExcluded", "BenchmarkUnexpected"),
                                   "missing-skip": "", "duplicate-skip": skipped * 2}[kind]
                    xml.write_text(xml.read_text().replace(skipped, replacement))
                self.assertEqual(self.pgo_verdict(invocation)["status"], "INCOMPLETE")
                shutil.rmtree(invocation)

    def test_pgo_requires_profile_identity_and_digest(self) -> None:
        for kind in ("missing-profile", "digest", "platform", "empty-profile", "comparison"):
            with self.subTest(kind=kind):
                invocation = self.pgo_fixture()
                metadata = invocation / "test.outputs/pgo/result.json"
                value = report.read_json(metadata)
                profile = invocation / "test.outputs/pgo/state.pgo.pprof.pb.gz"
                if kind == "missing-profile":
                    events = self.events(invocation)
                    result = next(event["testResult"] for event in events if "testResult" in event)
                    result["testActionOutput"] = [item for item in result["testActionOutput"] if not item["name"].endswith(".pb.gz")]
                    self.write_events(invocation, events)
                elif kind == "digest":
                    value["generated_sha256"] = "0" * 64
                elif kind == "platform":
                    value["platform"] = "kvm"
                elif kind == "empty-profile":
                    profile.write_bytes(b"")
                    value["generated_sha256"] = hashlib.sha256(b"").hexdigest()
                else:
                    value["comparison"] = ""
                report.write_json(metadata, value)
                self.assertEqual(self.pgo_verdict(invocation)["status"], "INCOMPLETE")
                shutil.rmtree(invocation)

    def test_pgo_preserves_all_xml_failures(self) -> None:
        for original in (True, False):
            with self.subTest(original_converter_error=original):
                invocation = self.pgo_fixture()
                xml = ((FIXTURES / "benchmark/test.xml").read_text() if original else
                       '<testsuites><testsuite><testcase name="BenchmarkFirstRestore/empty"><failure message="real benchmark failure"/></testcase></testsuite></testsuites>')
                (invocation / "test.xml").write_text(xml)
                self.assertEqual(self.pgo_verdict(invocation)["status"], "FAIL")
                shutil.rmtree(invocation)

    def test_benchmark_observations_are_not_named_tests(self) -> None:
        invocation = self.benchmark_fixture()
        value = self.benchmark_verdict(invocation)
        self.assertEqual(value["status"], "PASS", value)
        self.assertEqual(value["case_counts"], {"passed": 0, "failed": 0, "skipped": 0})
        self.assertEqual(value["executed"][0]["benchmarks"][0]["iterations"], 1)
        self.assertEqual(value["executed"][0]["benchmarks"][0]["metrics"]["wire-B/op"], 15)
        self.assertEqual(report.bundle(self.evidence, 0, 0, self.root / "bundle.tar.gz")["status"], "PASS")
        context = report.read_json(invocation / "context.json")
        context["command_kind"] = "test"
        report.write_json(invocation / "context.json", context)
        value = self.verdict(invocation, analysis=invocation / "expected-analysis.jsonl")
        self.assertEqual(value["status"], "INCOMPLETE", value)

    def test_benchmark_cpu_suffix_preserves_leaf_and_count_contracts(self) -> None:
        cases = (
            (None, "", ("",), "PASS"),
            (None, "", ("-3",), "PASS"),
            (None, "/case-32", ("/case-32",), "PASS"),
            (None, "/case-32", ("/case-32-3",), "PASS"),
            (None, "", ("-3", "-4"), "INCOMPLETE"),
            (None, "", ("/unexpected-3",), "INCOMPLETE"),
            ("1", "", ("-3",), "INCOMPLETE"),
            ("3", "-3", ("-3",), "PASS"),
            ("3", "-3", ("-4",), "INCOMPLETE"),
        )
        for cpu, expected_suffix, observed_suffixes, status in cases:
            with self.subTest(cpu=cpu, expected=expected_suffix, observed=observed_suffixes):
                invocation = self.benchmark_fixture()
                manifest_path = invocation / "benchmarks.json"
                manifest = report.read_json(manifest_path)
                manifest["arguments"].remove("-test.cpu=1")
                if cpu is not None:
                    manifest["arguments"].append("-test.cpu=" + cpu)
                (leaves,) = manifest["owners"].values()
                (leaf,) = leaves
                name = leaf["name"]
                leaf["name"] += expected_suffix
                report.write_json(manifest_path, manifest)
                arguments = invocation / "arguments.txt"
                original = arguments.read_text().replace("--test_arg=-test.cpu=1\n", "")
                arguments.write_text(original + (f"--test_arg=-test.cpu={cpu}\n" if cpu is not None else ""))
                path = invocation / "test.log"
                lines = path.read_text().splitlines(keepends=True)
                row = next(line for line in lines if "ns/op" in line)
                path.write_text("".join("".join(row.replace(name, name + suffix, 1) for suffix in observed_suffixes)
                                        if line == row else line for line in lines))
                value = self.benchmark_verdict(invocation)
                self.assertEqual(value["status"], status, value)
                self.assertEqual([row["name"] for row in value["executed"][0]["benchmarks"]],
                                 [name + suffix for suffix in observed_suffixes])
                shutil.rmtree(invocation)

    def test_pgo_numeric_leaf_suffix_is_not_a_second_cpu_suffix(self) -> None:
        invocation = self.pgo_fixture()
        name = "BenchmarkFirstRestore/case-32"
        manifest = invocation / "benchmarks.json"
        manifest.write_text(manifest.read_text().replace("BenchmarkFirstRestore/empty", name))
        log = invocation / "test.log"
        log.write_text(log.read_text().replace("BenchmarkFirstRestore/empty", name + "-3"))
        xml = invocation / "test.xml"
        xml.write_text(xml.read_text().replace("BenchmarkFirstRestore/empty", name))
        value = self.pgo_verdict(invocation)
        self.assertEqual(value["status"], "PASS", value)
        self.assertEqual(value["executed"][0]["benchmarks"][0]["name"], name)

    def test_benchmark_missing_extra_zero_and_invalid_rows(self) -> None:
        changes = {
            "missing": lambda row: "",
            "extra": lambda row: row + row,
            "unexpected-leaf": lambda row: row.replace("/empty", "/unexpected"),
            "zero-iterations": lambda row: row.replace("\t       1\t", "\t       0\t"),
            "wrong-iterations": lambda row: row.replace("\t       1\t", "\t       2\t"),
            "missing-metric": lambda row: row.replace("allocs/op", "other/op"),
            "nonfinite-metric": lambda row: row.replace("7090", "NaN"),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                invocation = self.benchmark_fixture()
                path = invocation / "test.log"
                lines = path.read_text().splitlines(keepends=True)
                row = next(line for line in lines if "ns/op" in line)
                updated = change(row)
                self.assertNotEqual(updated, row, name)
                path.write_text("".join(updated if line == row else line for line in lines))
                self.assertEqual(self.benchmark_verdict(invocation)["status"], "INCOMPLETE")
                shutil.rmtree(invocation)

    def test_benchmark_completion_with_appended_stderr(self) -> None:
        tail = (FIXTURES / "benchmark/docker-stderr.txt").read_text()
        for pgo, kind in itertools.product((False, True),
                                           ("stderr", "missing-pass", "substring", "duplicate", "failure", "after-pass")):
            with self.subTest(pgo=pgo, kind=kind):
                invocation = self.pgo_fixture() if pgo else self.benchmark_fixture()
                path = invocation / "test.log"
                original = path.read_text()
                row = next(line for line in original.splitlines(keepends=True) if "ns/op" in line)
                logs = {
                    "stderr": original,
                    "missing-pass": original.replace("PASS\n", ""),
                    "substring": original.replace("PASS\n", "prefix PASS suffix\n"),
                    "duplicate": original + "PASS\n",
                    "failure": original + "FAIL\n",
                    "after-pass": original + row,
                }
                path.write_text(logs[kind] + tail)
                value = self.pgo_verdict(invocation) if pgo else self.benchmark_verdict(invocation)
                self.assertEqual(value["status"], "PASS" if kind == "stderr" else "INCOMPLETE", value)
                shutil.rmtree(invocation)

    def test_benchmark_failure_remains_primary(self) -> None:
        invocation = self.benchmark_fixture()
        (invocation / "native-exit.txt").write_text("3\n")
        (invocation / "test.log").write_text("--- FAIL: BenchmarkFirstRestore/empty\nFAIL\n")
        value = self.benchmark_verdict(invocation)
        self.assertEqual((value["status"], value["native_exit"]), ("FAIL", 3))

    def test_benchmark_preserves_original_xml_errors(self) -> None:
        invocation = self.benchmark_fixture()
        shutil.copyfile(FIXTURES / "benchmark/test.xml", invocation / "test.xml")
        value = self.benchmark_verdict(invocation)
        self.assertEqual(value["status"], "FAIL", value)
        self.assertEqual(value["case_counts"]["failed"], 2)

    def test_benchmark_requires_bep_success(self) -> None:
        invocation = self.benchmark_fixture()
        events = self.events(invocation)
        for event in events:
            if "testResult" in event:
                event["testResult"]["status"] = "FAILED"
        self.write_events(invocation, events)
        self.assertEqual(self.benchmark_verdict(invocation)["status"], "FAIL")

    def test_benchmark_does_not_hide_named_tests(self) -> None:
        invocation = self.benchmark_fixture()
        (invocation / "test.xml").write_text('<testsuite><testcase name="TestUnexpected"/></testsuite>')
        self.assertEqual(self.benchmark_verdict(invocation)["status"], "INCOMPLETE")

    def test_benchmark_manifest_and_package_completion_required(self) -> None:
        for change in ("missing-manifest", "wrong-owner", "missing-pass", "different-arguments"):
            with self.subTest(change=change):
                invocation = self.benchmark_fixture()
                manifest = invocation / "benchmarks.json"
                if change == "missing-manifest":
                    manifest.unlink()
                elif change == "wrong-owner":
                    value = report.read_json(manifest)
                    value["owners"] = {"//other:benchmark": next(iter(value["owners"].values()))}
                    report.write_json(manifest, value)
                elif change == "missing-pass":
                    path = invocation / "test.log"
                    path.write_text(path.read_text().replace("PASS\n", ""))
                else:
                    path = invocation / "arguments.txt"
                    path.write_text(path.read_text().replace("-test.v=false", "-test.v=true"))
                self.assertEqual(self.benchmark_verdict(invocation)["status"], "INCOMPLETE")
                shutil.rmtree(invocation)

    def test_benchmark_manifest_rejects_ordinary_test_arguments(self) -> None:
        invocation = self.benchmark_fixture()
        path = invocation / "benchmarks.json"
        original = report.read_json(path)
        for argument in ("-test.run=Test", "-test.v=true", "--config=local"):
            with self.subTest(argument=argument):
                value = dict(original, arguments=original["arguments"] + [argument])
                report.write_json(path, value)
                with self.assertRaises(ValueError):
                    report.benchmark_manifest(path)

    def lifecycle_fixture(self, package: str = "runsc") -> Path:
        invocation = self.mixed_fixture("<testsuites/>")
        owner = f'//{"runsc/container" if package == "runsc" else "shim/tests"}:lifecycle_benchmark_systrap_test'
        events = json.loads(json.dumps(self.events(invocation)).replace("//another:test", owner))
        for event in events:
            if event.get("id", {}).get("targetConfigured", {}).get("label") == owner:
                event["configured"]["tag"].append("go-benchmark-contract")
        self.write_events(invocation, events)
        expected = invocation / "expected.txt"
        expected.write_text(expected.read_text().replace("//another:test", owner))
        shutil.copyfile(FIXTURES / "lifecycle" / f"{package}.log", invocation / "another/test.log")
        return invocation

    def lifecycle_verdict(self, invocation: Path) -> dict[str, object]:
        self.assertEqual(self.capture(invocation)["errors"], [])
        return self.verdict(invocation, analysis=self.analysis(invocation), declared_benchmarks=LIFECYCLE_MANIFEST)

    def test_declared_benchmarks_in_mixed_invocation(self) -> None:
        for package, rows in (("runsc", 16), ("shim", 22)):
            with self.subTest(package=package):
                invocation = self.lifecycle_fixture(package)
                value = self.lifecycle_verdict(invocation)
                self.assertEqual(value["status"], "PASS", value)
                self.assertEqual(value["case_counts"], {"passed": 45, "failed": 0, "skipped": 4})
                self.assertEqual(len(value["zero_case_owners"]), 1)
                measured = [item for item in value["executed"] if "benchmarks" in item]
                self.assertEqual(len(measured), 1)
                self.assertEqual(len(measured[0]["benchmarks"]), rows)
                self.assertTrue(all(row["iterations"] == 30 for row in measured[0]["benchmarks"]))
                shutil.rmtree(invocation)

    def test_declared_benchmarks_reject_incomplete_or_failed_execution(self) -> None:
        for change in ("missing", "duplicate", "unexpected", "iterations", "metrics", "malformed", "missing-pass",
                       "untagged", "missing-manifest", "ordinary-zero", "result-failure", "summary-failure", "xml-failure"):
            with self.subTest(change=change):
                invocation = self.lifecycle_fixture()
                path = invocation / "another/test.log"
                lines = path.read_text().splitlines(keepends=True)
                row = next(line for line in lines if "ns/op" in line)
                replacements = {
                    "missing": "", "duplicate": row + row,
                    "unexpected": row.replace("/systrap", "/unexpected"),
                    "iterations": row.replace("30\t", "29\t"),
                    "metrics": row.replace("p50.ns", "other.ns"),
                    "malformed": row.replace("30\t", "invalid\t"),
                }
                if change in replacements:
                    self.assertNotEqual(replacements[change], row)
                    path.write_text("".join(replacements[change] if line == row else line for line in lines))
                elif change == "missing-pass":
                    path.write_text(path.read_text().replace("PASS\n", ""))
                elif change == "ordinary-zero":
                    (invocation / "test.xml").write_text("<testsuites/>")
                elif change == "xml-failure":
                    (invocation / "another/test.xml").write_text('<testsuite><testcase name="BenchmarkOCICreate/systrap"><failure/></testcase></testsuite>')
                else:
                    events = self.events(invocation)
                    for event in events:
                        if change == "untagged" and "configured" in event:
                            event["configured"]["tag"] = [tag for tag in event["configured"]["tag"] if tag != "go-benchmark-contract"]
                        if "lifecycle_benchmark" in json.dumps(event.get("id")):
                            if change == "result-failure" and "testResult" in event:
                                event["testResult"]["status"] = "FAILED"
                            if change == "summary-failure" and "testSummary" in event:
                                event["testSummary"]["overallStatus"] = "FAILED"
                    self.write_events(invocation, events)
                if change == "missing-manifest":
                    self.capture(invocation)
                    value = self.verdict(invocation, analysis=self.analysis(invocation))
                else:
                    value = self.lifecycle_verdict(invocation)
                self.assertEqual(value["status"], "FAIL" if change.endswith("failure") else "INCOMPLETE", value)
                shutil.rmtree(invocation)

    def test_declared_benchmark_tag_does_not_change_other_command_kinds(self) -> None:
        for kind in ("build", "analysis", "benchmark"):
            with self.subTest(kind=kind):
                invocation = self.benchmark_fixture() if kind == "benchmark" else self.fixture()
                if kind == "analysis":
                    (invocation / "events.jsonl").write_bytes((FIXTURES / "analysis-no-build/events.jsonl").read_bytes())
                    (invocation / "expected.txt").write_text("")
                    (invocation / "native-exit").write_text("1\n")
                    (invocation / "executed-arguments.txt").write_text("test\n--nobuild\n")
                events = self.events(invocation)
                if kind == "build":
                    events = [event for event in events if "testResult" not in event and "testSummary" not in event]
                    for event in events:
                        if "children" in event:
                            event["children"] = [child for child in event["children"] if "testResult" not in child and "testSummary" not in child]
                    (invocation / "executed-arguments.txt").write_text("build\n//...\n")
                for event in events:
                    if "configured" in event:
                        event["configured"].setdefault("tag", []).append("go-benchmark-contract")
                self.write_events(invocation, events)
                context = report.read_json(invocation / "context.json")
                context["command_kind"] = kind
                report.write_json(invocation / "context.json", context)
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation, analysis=invocation / "events.jsonl",
                                     benchmarks=invocation / "benchmarks.json" if kind == "benchmark" else None)
                self.assertEqual(value["status"], "PASS", value)
                shutil.rmtree(invocation)

    def test_rfc3339_timestamp_precision(self) -> None:
        expected = datetime.datetime(2026, 10, 16, 17, 39, 9, 185849, datetime.timezone.utc)
        self.assertEqual(report.timestamp("2026-10-16T17:39:09.1858490Z"), expected)
        self.assertEqual(report.timestamp("2026-10-16T17:39:09.185849+00:00"), expected)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            report.timestamp("2026-10-16T17:39:09")

    def test_filtered_save_keeps_build_only_test_roots(self) -> None:
        invocation = self.fixture("filtered-save")
        self.assertEqual(self.capture(invocation)["errors"], [])
        value = self.verdict(invocation, analysis=invocation / "expected-analysis.jsonl")
        self.assertEqual(value["status"], "PASS", value)
        self.assertEqual(value["errors"], [])
        self.assertEqual(value["missing"], [])
        self.assertEqual(len(value["expected"]), 1)
        self.assertEqual(len(value["executed"]), 1)

    def test_filtered_save_rejects_unexpected_execution(self) -> None:
        invocation = self.fixture("filtered-save")
        events = self.events(invocation)
        selected = (invocation / "expected.txt").read_text().strip()
        extra = "//test/syscalls:32bit_test_native"
        result = next(event for event in events if "testResult" in event)
        summary = next(event for event in events if "testSummary" in event)
        unexpected = json.loads(json.dumps([result, summary]).replace(selected, extra))
        for event in events:
            if event.get("id", {}).get("targetCompleted", {}).get("label") == extra:
                event["children"] = [row["id"] for row in unexpected]
        self.write_events(invocation, unexpected + events)
        self.assertEqual(self.capture(invocation)["errors"], [])
        value = self.verdict(invocation, analysis=invocation / "expected-analysis.jsonl")
        self.assertEqual(value["status"], "INCOMPLETE", value)
        self.assertIn("Results or summaries belong to an unexpected configuration", value["errors"])

    def test_filtered_save_requires_selected_owner(self) -> None:
        invocation = self.fixture("filtered-save")
        selected = (invocation / "expected.txt").read_text().strip()
        self.write_events(invocation, [event for event in self.events(invocation)
                                       if not any(identity.get("label") == selected
                                                  for identity in event["id"].values())])
        self.assertEqual(self.capture(invocation)["errors"], [])
        value = self.verdict(invocation, analysis=invocation / "expected-analysis.jsonl")
        self.assertEqual(value["status"], "INCOMPLETE", value)
        self.assertEqual({row["label"] for row in value["missing"]}, {selected})
        self.assertTrue(any("Configured owners differ" in error for error in value["errors"]), value)

    def test_filtered_save_preserves_build_only_failure(self) -> None:
        invocation = self.fixture("filtered-save")
        events = self.events(invocation)
        extra = "//test/syscalls:32bit_test_native"
        for event in events:
            if event.get("id", {}).get("targetCompleted", {}).get("label") == extra:
                event["completed"]["success"] = False
        self.write_events(invocation, events)
        (invocation / "native-exit.txt").write_text("1\n")
        self.assertEqual(self.capture(invocation)["errors"], [])
        value = self.verdict(invocation, analysis=invocation / "expected-analysis.jsonl")
        self.assertEqual((value["status"], value["native_exit"]), ("FAIL", 1))
        self.assertEqual([row["label"] for row in value["failures"]], [extra])

    def image_command_files(self, architecture="x86_64"):
        images = ["basic_alpine", "benchmarks_profile-helper"]
        command = {"owner": "//test/docker:images", "architecture": architecture, "image_class": "cpu", "images": []}
        selection = {"schema_version": 1, "architecture": architecture, "image_class": "cpu",
                     "partition": 1, "partitions": 1, "cohort": images, "selected": images, "images": images}
        files = {"source-images/selection.json": json.dumps(selection).encode()}
        for image, operation in zip(images, ("docker-build", "archive-load"), strict=True):
            reference = "gvisor.dev/images/" + image.replace("_", "/") + ":" + "a" * 16
            path = "source-images/" + image + "/"
            files[path + "request.json"] = json.dumps({
                "schema_version": 1, "image": image, "architecture": architecture, "operation": operation,
                "reference": reference, "archive_sha256": "b" * 64 if operation == "archive-load" else None}).encode()
            files[path + "build-exit-code"] = files[path + "exit-code"] = b"0\n"
            files[path + "inspect.json"] = json.dumps([{
                "Id": "sha256:" + "c" * 64, "RepoTags": [reference], "Os": "linux",
                "Architecture": {"x86_64": "amd64", "aarch64": "arm64"}[architecture]}]).encode()
        return command, files

    def image_command_reports(self, attempt, files, zipped=False):
        directory = Path(tempfile.mkdtemp(dir=self.root))
        if zipped:
            path = directory / "outputs.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in files.items():
                    archive.writestr(name, data)
            return {(attempt, "test.outputs__outputs.zip"): path}
        reports = {}
        for name, data in files.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            reports[attempt, "test.outputs/" + name] = path
        return reports

    def dynamic_image_fixture(self, *, zipped: bool = False, archive_sha256: str | None = None) -> Path:
        invocation = self.native_image_fixture()
        contract = report.read_json(invocation / "expectations.json")
        configured, = contract["configured_owners"]
        image, = configured["images"]
        canonical = report.read_json(invocation / "verdict.json")
        canonical["command_kind"] = "test"
        report.write_json(invocation / "verdict.json", canonical)
        contract.pop("benchmarks")
        contract["owners"] = {configured["label"]: []}
        command, files = self.image_command_files()
        files = {name.replace("benchmarks_profile-helper", "benchmarks_iperf"):
                 data.replace(b"benchmarks_profile-helper", b"benchmarks_iperf").replace(
                     b"benchmarks/profile-helper", b"benchmarks/iperf") for name, data in files.items()}
        records, spawns = report.compact_records(invocation / "execution.json")
        archive_id, = [identifier for identifier, row in records.items()
                      if row.get("file", {}).get("path") == image["archive"]]
        context_id, context_inputs_id = range(max(records) + 1, max(records) + 3)
        context = "bazel-out/k8-fastbuild/bin/images/cpu_source_contexts.tar"
        records.update({
            context_id: {"id": context_id, "file": {"path": context, "digest": {"hash": "d" * 64, "sizeBytes": "100"}}},
            context_inputs_id: {"id": context_inputs_id, "inputSet": {"inputIds": [archive_id]}},
        })
        test, = [spawn for spawn in spawns if spawn.get("mnemonic") == "TestRunner"]
        for row in records.values():
            inputs = row.get("inputSet", {}).get("inputIds", [])
            if archive_id in inputs and row["id"] != context_inputs_id:
                inputs[inputs.index(archive_id)] = context_id
        spawns.append({"targetLabel": "//images:cpu_source_contexts", "mnemonic": "PackageTar",
                       "inputSetId": context_inputs_id, "outputs": [{"outputId": context_id}], "exitCode": 0})
        command.update(contexts=context, context_images=[image])
        configured.update(images=[], source_commands=[command])
        report.write_json(invocation / "expectations.json", contract)
        request_path = "source-images/benchmarks_iperf/request.json"
        request = json.loads(files[request_path])
        request["archive_sha256"] = records[archive_id]["file"]["digest"]["hash"] if archive_sha256 is None else archive_sha256
        files[request_path] = json.dumps(request).encode()
        directory, = [records[output["outputId"]]["directory"] for output in test["outputs"]
                      if "outputId" in output and "directory" in records[output["outputId"]]]
        directory["files"] = []
        manifest = report.read_json(invocation / "manifest.json")
        identity = manifest["reports"][0]["identity"]
        if zipped:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                for name, data in files.items():
                    archive.writestr(name, data)
            files = {"outputs.zip": buffer.getvalue()}
        for name, data in files.items():
            path = invocation / "reports" / directory["path"].removeprefix("bazel-out/") / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            digest = report.file_record(path)
            manifest["reports"].append({"identity": identity, "name": "test.outputs__outputs.zip" if zipped else "test.outputs/" + name,
                                        "path": str(path.relative_to(invocation)), **digest})
            directory["files"].append({"path": name, "digest": {"hash": digest["sha256"], "sizeBytes": str(digest["bytes"])}})
        report.write_json(invocation / "manifest.json", manifest)
        (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in
            [*records.values(), *({"spawn": spawn} for spawn in spawns)]))
        return invocation

    def test_dynamic_images_require_context_and_output_provenance(self) -> None:
        for damage, expected in (("", ""), ("zip", ""),
                                 ("context", "Missing configured image context"),
                                 ("producer", "Archive is not a context input"),
                                 ("ambiguous", "Ambiguous input producer"),
                                 ("failed", "Unsuccessful input producer"),
                                 ("archive", "Loaded archive differs from configured context"),
                                 ("output", "Image-build result differs from TestRunner output")):
            with self.subTest(damage=damage):
                invocation = self.dynamic_image_fixture(zipped=damage == "zip", archive_sha256="e" * 64 if damage == "archive" else None)
                try:
                    contract = report.read_json(invocation / "expectations.json")
                    records, spawns = report.compact_records(invocation / "execution.json")
                    producer, = [spawn for spawn in spawns if spawn.get("mnemonic") == "PackageTar"]
                    if damage == "context":
                        contract["configured_owners"][0]["source_commands"][0]["contexts"] += ".unrelated"
                    elif damage == "producer":
                        spawns.remove(producer)
                    elif damage == "ambiguous":
                        spawns.append(producer.copy())
                    elif damage == "failed":
                        producer["exitCode"] = 1
                    elif damage == "output":
                        directory, = [row["directory"] for row in records.values()
                                      if row.get("directory", {}).get("path", "").endswith("/test.outputs")]
                        directory["files"][0]["digest"]["hash"] = "0" * 64
                    report.write_json(invocation / "expectations.json", contract)
                    (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in
                        [*records.values(), *({"spawn": spawn} for spawn in spawns)]))
                    value = self.source_image_audit(invocation)
                    if expected:
                        self.assertEqual(value["audit_status"], "INCOMPLETE", value)
                        self.assertIn(expected, "\n".join(value["errors"]))
                    else:
                        self.assertEqual((value["audit_status"], value["errors"]), ("PASS", []), value)
                        consumer, = value["consumers"]
                        self.assertEqual([row["operation"] for row in consumer["image_build"]["results"]],
                                         ["docker-build", "archive-load"])
                finally:
                    shutil.rmtree(invocation)

    def test_image_results_require_exact_selection_and_successful_outputs(self) -> None:
        attempt = report.Attempt(report.Owner("//test:images", "configuration"), 1, 1, 1)
        for architecture, zipped in itertools.product(("x86_64", "aarch64"), (False, True)):
            with self.subTest(architecture=architecture, zipped=zipped):
                command, files = self.image_command_files(architecture)
                result = report.image_command_result(attempt, self.image_command_reports(attempt, files, zipped), command)
                self.assertEqual([row["operation"] for row in result["results"]], ["docker-build", "archive-load"])
                report.image_command_partitions([result])
        for damage, expected in (
                ("selection", "selection differs from its declared cohort"),
                ("missing", "results are missing or unexpected"),
                ("build", "build or result collection failed"),
                ("collection", "build or result collection failed"),
                ("platform", "identity or platform differs"),
                ("tag", "identity or platform differs"),
                ("archive", "Invalid image-build archive identity")):
            with self.subTest(damage=damage):
                command, files = self.image_command_files()
                if damage == "selection":
                    command["images"] = ["basic_alpine"]
                elif damage == "missing":
                    files.pop("source-images/basic_alpine/exit-code")
                elif damage in ("build", "collection"):
                    name = "build-exit-code" if damage == "build" else "exit-code"
                    files["source-images/basic_alpine/" + name] = b"17\n"
                elif damage in ("platform", "tag"):
                    path = "source-images/basic_alpine/inspect.json"
                    value = json.loads(files[path])
                    value[0]["Architecture" if damage == "platform" else "RepoTags"] = "arm64" if damage == "platform" else []
                    files[path] = json.dumps(value).encode()
                else:
                    path = "source-images/benchmarks_profile-helper/request.json"
                    value = json.loads(files[path])
                    value["archive_sha256"] = "unknown"
                    files[path] = json.dumps(value).encode()
                with self.assertRaisesRegex(ValueError, expected):
                    report.image_command_result(attempt, self.image_command_reports(attempt, files), command)

    def test_image_shards_must_cover_the_complete_cohort_once(self) -> None:
        _, files = self.image_command_files()
        selection = json.loads(files["source-images/selection.json"])
        first, second = selection["images"]
        rows = [selection | {"partitions": 2, "partition": index, "images": [image]}
                for index, image in enumerate((first, second), 1)]
        report.image_command_partitions(rows)
        for broken in ([], rows[:1], rows + rows[:1],
                       [rows[0], rows[1] | {"images": [first]}],
                       [rows[0], rows[1] | {"selected": [second]}]):
            with self.subTest(rows=broken), self.assertRaisesRegex(ValueError, "Missing image-build shards|shards differ"):
                report.image_command_partitions(broken)

    def test_retained_test_no_build_preflight(self) -> None:
        events = FIXTURES / "analysis-no-build/events.jsonl"
        value = report.preflight(events, 1, "test")
        self.assertEqual((value["status"], value["native_exit"]), ("PASS", 1))
        self.assertEqual(len(value["expected"]), 2)
        self.assertEqual(value["output_options"], [])
        self.assertEqual(report.preflight(events, 1, "build")["status"], "INCOMPLETE")
        self.assertEqual(report.preflight(events, 4, "test")["status"], "INCOMPLETE")
        for tag, retained, omitted in (
                ("jtreg-results", "batch-0.log", "source-images/selection.json"),
                ("nodejs-results", "batch-0.log", "source-images/selection.json"),
                ("nodejs-exact-results", "batch-0.log", "source-images/selection.json"),
                ("source-image-results", "source-images/selection.json", "batch-0.log")):
            with self.subTest(tag=tag):
                records = [json.loads(line) for line in events.read_text().splitlines()]
                configured = next(record for record in records if "configured" in record)
                configured["configured"].setdefault("tag", []).append(tag)
                path = self.root / f"{tag}-analysis.jsonl"
                path.write_text("".join(json.dumps(record) + "\n" for record in records))
                value = report.preflight(path, 1, "test")
                self.assertEqual(value["status"], "PASS", value)
                self.assertIn("--nozip_undeclared_test_outputs", value["output_options"])
                [pattern] = [option.removeprefix("--remote_download_regex=")
                             for option in value["output_options"] if option.startswith("--remote_download_regex=")]
                label = configured["id"]["targetConfigured"]["label"]
                output = "bazel-out/config/testlogs/" + label.removeprefix("//").replace(":", "/")
                self.assertRegex(output + "/run_1_of_2/test.outputs/" + retained, pattern)
                self.assertIsNone(re.search(pattern, output + "/test.outputs/" + omitted))
                self.assertIsNone(re.search(pattern, output + "/test.outputs/boot.log"))

    def test_test_no_build_requires_all_owners_and_no_failure(self) -> None:
        original = [json.loads(line) for line in (FIXTURES / "analysis-no-build/events.jsonl").read_text().splitlines()]
        no_build = next(event for event in original if event.get("aborted", {}).get("reason") == "NO_BUILD")
        variants = {
            "missing-owner": [event for event in original if event is not no_build],
            "analysis-failure": original + [{"id": {"pattern": {"pattern": ["//missing:target"]}}, "aborted": {"reason": "ANALYSIS_FAILURE"}}],
            "no-owners": [event for event in original if "configured" not in event],
            "test-executed": original + [{"id": {}, "testResult": {"status": "PASSED"}}],
        }
        for name, events in variants.items():
            with self.subTest(name=name):
                path = self.root / (name + ".jsonl")
                path.write_text("".join(json.dumps(event) + "\n" for event in events))
                self.assertEqual(report.preflight(path, 1, "test")["status"], "INCOMPLETE")

    def test_analysis_verdict_retains_accepted_original_exit(self) -> None:
        invocation = self.fixture()
        (invocation / "events.jsonl").write_bytes((FIXTURES / "analysis-no-build/events.jsonl").read_bytes())
        events = self.events(invocation)
        for event in events:
            if "configured" in event:
                event["configured"].setdefault("tag", []).append("qualification-output-group=default")
        self.write_events(invocation, events)
        (invocation / "expected.txt").write_text("")
        (invocation / "native-exit").write_text("1\n")
        (invocation / "executed-arguments.txt").write_text("test\n--nobuild\n")
        context = report.read_json(invocation / "context.json")
        context["command_kind"] = "analysis"
        report.write_json(invocation / "context.json", context)
        self.capture(invocation)
        value = self.verdict(invocation, analysis=invocation / "events.jsonl")
        self.assertEqual((value["status"], value["native_exit"], value["accepted_analysis_exit"]), ("PASS", 1, True))
        combined = report.bundle(self.evidence, 0, 0, self.root / "bundle.tar.gz")
        self.assertEqual((combined["status"], combined["native_exit"]), ("PASS", 0))
        self.assertEqual(combined["native_exits"][0]["exit"], 1)

    def test_retained_failure_preserves_missing_shards(self) -> None:
        invocation = self.fixture("fail")
        self.assertEqual(self.capture(invocation)["errors"], [])
        manifest = invocation / "required.json"
        case = "container/TestMountEROFS"
        label = (invocation / "expected.txt").read_text().strip()
        report.write_json(manifest, {label: [case]})
        value = self.verdict(invocation, case_manifest=manifest)
        self.assertEqual(value["status"], "FAIL")
        self.assertEqual(value["native_exit"], 3)
        self.assertEqual(value["case_counts"]["failed"], 1)
        self.assertEqual(sum(item.get("reason") == "announced result missing" for item in value["missing"]), 7)
        self.assertNotIn(case, [item.get("name") for item in value["missing"]])
        failed, = value["unpassed_required_cases"]
        self.assertEqual((failed["name"], failed["outcomes"]), (case, ["failed"]))
        self.assertEqual(any(item["reason"] == "XML test failures" for item in value["failures"]), True)

    def test_expected_failure_requires_declared_passing_wrapper(self) -> None:
        for declared, wrapper_passes, child in (
            (True, True, "failed"),
            (False, True, "failed"),
            (True, False, "failed"),
            (True, True, "unnamed"),
            (True, True, "passed"),
            (True, True, "skipped"),
            (True, True, "zero"),
        ):
            with self.subTest(declared=declared, wrapper_passes=wrapper_passes, child=child):
                invocation = self.fixture()
                events = self.events(invocation)
                for event in events:
                    if declared and "configured" in event:
                        event["configured"]["tag"].append("expected-failure")
                    if not wrapper_passes:
                        if "testResult" in event:
                            event["testResult"]["status"] = "FAILED"
                        if "testSummary" in event:
                            event["testSummary"]["overallStatus"] = "FAILED"
                self.write_events(invocation, events)
                xml = {
                    "failed": '<testcase name="Unsupported"><failure message="known defect"/></testcase>',
                    "unnamed": '<testcase><failure message="setup error"/></testcase>',
                    "passed": '<testcase name="Unsupported"/>',
                    "skipped": '<testcase name="Unsupported"><skipped/></testcase>',
                    "zero": "",
                }[child]
                (invocation / "test.xml").write_text("<testsuite>" + xml + "</testsuite>")
                self.assertEqual(self.capture(invocation)["errors"], [])
                analysis = self.analysis(invocation)
                value = self.verdict(invocation, analysis=analysis)
                accepted = declared and wrapper_passes and child == "failed"
                failed = child in ("failed", "unnamed")
                status = "PASS" if accepted else "FAIL" if failed else "INCOMPLETE"
                self.assertEqual(value["status"], status, value)
                self.assertEqual(value["case_counts"]["failed"], int(failed))
                self.assertEqual(len(value["expected_failures"]), int(accepted))
                if accepted:
                    self.assertEqual(value["case_counts"]["passed"], 0)
                    combined = report.bundle(self.evidence, 0, 0, self.root / "bundle.tar.gz")
                    self.assertEqual(combined["expected_failures"], value["expected_failures"])
                    manifest = invocation / "required.json"
                    label = (invocation / "expected.txt").read_text().strip()
                    report.write_json(manifest, {label: ["Unsupported"]})
                    value = self.verdict(invocation, analysis=analysis, case_manifest=manifest)
                    self.assertEqual((value["status"], value["missing"]), ("INCOMPLETE", []))
                    combined = report.bundle(self.evidence, 0, 0, self.root / "required.tar.gz")
                    case, = combined["unpassed_required_cases"]
                    self.assertEqual((case["name"], case["outcomes"]), ("Unsupported", ["failed"]))
                shutil.rmtree(invocation)

    def test_retained_absent_results(self) -> None:
        invocation = self.fixture("incomplete")
        self.assertEqual(self.capture(invocation)["errors"], ["No complete build events"])
        value = self.verdict(invocation)
        self.assertEqual((value["status"], value["native_exit"]), ("FAIL", 1))
        self.assertEqual(len(value["missing"]), 34)
        # A falsely successful native exit still cannot turn absence into PASS.
        (invocation / "native-exit").write_text("0\n")
        self.assertEqual(self.verdict(invocation)["status"], "INCOMPLETE")

    def test_retained_zero_case_wrapper_is_incomplete(self) -> None:
        invocation = self.fixture("zero")
        self.capture(invocation)
        value = self.verdict(invocation)
        self.assertEqual((value["status"], value["native_exit"]), ("INCOMPLETE", 0))
        self.assertEqual(len(value["zero_case_owners"]), 1)
        self.assertEqual(value["case_counts"], {"passed": 0, "failed": 0, "skipped": 0})

    def marshal_fixture(self, contract: object, xml: str = "", *, declared: bool = True) -> Path:
        invocation = self.fixture()
        events = self.events(invocation)
        for event in events:
            if declared and "configured" in event:
                event["configured"]["tag"].append("go-marshal-test-contract")
        self.write_events(invocation, events)
        (invocation / "test.xml").write_text("<testsuite>" + xml + "</testsuite>")
        (invocation / "test.log").write_text("\x16gvisor-go-marshal-test: " + json.dumps(contract) + "\nPASS\n")
        return invocation

    def test_generated_abi_applicability_retains_zero_case_evidence(self) -> None:
        for count, matches in ((0, True), (0, False), (3, False)):
            with self.subTest(runtime_types=count, constraints_match=matches):
                contract = {"schema_version": 1, "runtime_types": count, "constraints_match": matches}
                invocation = self.marshal_fixture(contract)
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], "PASS", value)
                self.assertEqual(value["case_counts"], {"passed": 0, "failed": 0, "skipped": 0})
                self.assertEqual(len(value["zero_case_owners"]), 1)
                self.assertEqual(value["non_runtime_owners"], [{**value["zero_case_owners"][0], **contract}])
                artifact = self.root / "bundle.tar.gz"
                report.bundle(self.evidence, 0, 0, artifact)
                receipt = report.receipt(artifact, report.file_record(artifact)["sha256"], value["source"])
                self.assertEqual(receipt["non_runtime_owners"], value["non_runtime_owners"])
                shutil.rmtree(invocation)

    def test_generated_abi_contract_cannot_hide_runtime_coverage(self) -> None:
        for count, matches, declared, xml, status in (
            (2, True, True, "", "INCOMPLETE"),
            (2, True, True, '<testcase name="TestABI"><skipped/></testcase>', "INCOMPLETE"),
            (2, True, True, '<testcase name="TestABI"/>', "PASS"),
            (0, True, False, "", "INCOMPLETE"),
            (0, True, True, '<testcase name="TestABI"/>', "INCOMPLETE"),
            (2, False, True, '<testcase name="TestABI"><skipped/></testcase>', "INCOMPLETE"),
        ):
            with self.subTest(runtime_types=count, matches=matches, declared=declared, xml=xml):
                invocation = self.marshal_fixture(
                    {"schema_version": 1, "runtime_types": count, "constraints_match": matches}, xml, declared=declared)
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], status, value)
                self.assertEqual(value["non_runtime_owners"], [])
                shutil.rmtree(invocation)

    def test_generated_abi_contract_requires_one_typed_record(self) -> None:
        valid = {"schema_version": 1, "runtime_types": 0, "constraints_match": True}
        variants = [None, {}, {**valid, "schema_version": True}, {**valid, "schema_version": 2},
                    {**valid, "runtime_types": True}, {**valid, "runtime_types": -1},
                    {**valid, "runtime_types": "0"}, {**valid, "constraints_match": 1}]
        for contract in variants + ["missing", "malformed", "duplicate"]:
            with self.subTest(contract=contract):
                invocation = self.marshal_fixture(contract, '<testcase name="TestABI"/>')
                log = invocation / "test.log"
                if contract == "missing":
                    log.write_text("PASS\n")
                elif contract == "malformed":
                    log.write_text("gvisor-go-marshal-test: {\n")
                elif contract == "duplicate":
                    log.write_text(("gvisor-go-marshal-test: " + json.dumps(valid) + "\n") * 2)
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], "INCOMPLETE", value)
                self.assertEqual(value["non_runtime_owners"], [])
                shutil.rmtree(invocation)

    def test_generated_abi_contract_preserves_native_and_xml_failures(self) -> None:
        for failure in ("attempt", "summary", "xml-with-invalid-contract"):
            with self.subTest(failure=failure):
                invocation = self.marshal_fixture({"schema_version": 1, "runtime_types": 0, "constraints_match": True})
                events = self.events(invocation)
                for event in events:
                    if failure == "attempt" and "testResult" in event:
                        event["testResult"]["status"] = "FAILED"
                    if failure == "summary" and "testSummary" in event:
                        event["testSummary"]["overallStatus"] = "FAILED"
                self.write_events(invocation, events)
                if failure == "xml-with-invalid-contract":
                    (invocation / "test.log").write_text("FAIL\n")
                    (invocation / "test.xml").write_text('<testsuite><testcase name="TestABI"><failure/></testcase></testsuite>')
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], "FAIL", value)
                self.assertEqual(value["non_runtime_owners"], [])
                shutil.rmtree(invocation)

    def test_generated_abi_contract_must_cover_every_attempt_consistently(self) -> None:
        for second_count in (0, 2, None):
            with self.subTest(second_runtime_types=second_count):
                contract = {"schema_version": 1, "runtime_types": 0, "constraints_match": False}
                invocation = self.marshal_fixture(contract)
                second = invocation / "second"
                second.mkdir()
                shutil.copyfile(invocation / "test.xml", second / "test.xml")
                (second / "test.log").write_text("PASS\n" if second_count is None else
                                               "gvisor-go-marshal-test: " + json.dumps({**contract, "runtime_types": second_count}) + "\n")
                events = self.events(invocation)
                extra = json.loads(json.dumps(next(event for event in events if "testResult" in event)))
                extra["id"]["testResult"]["attempt"] = 2
                for output in extra["testResult"]["testActionOutput"]:
                    output["uri"] = (second / output["name"]).as_uri()
                for event in events:
                    if "completed" in event:
                        event["children"].append(extra["id"])
                    if "testSummary" in event:
                        event["testSummary"]["totalRunCount"] = 2
                        event["testSummary"]["attemptCount"] = 2
                self.write_events(invocation, events + [extra])
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual(value["status"], "PASS" if second_count == 0 else "INCOMPLETE", value)
                self.assertEqual(len(value["non_runtime_owners"]), int(second_count == 0))
                shutil.rmtree(invocation)

    def test_changed_configuration_is_not_same_owner(self) -> None:
        invocation = self.fixture()
        analysis = self.analysis(invocation)
        events = self.events(invocation)
        configured = next(event for event in events if "configured" in event)
        completed = next(child["targetCompleted"] for child in configured["children"] if "targetCompleted" in child)
        configuration = completed["configuration"]["id"]
        self.write_events(invocation, json.loads(json.dumps(events).replace(configuration, "different-configuration")))
        self.capture(invocation)
        value = self.verdict(invocation, analysis=analysis)
        self.assertEqual(value["status"], "INCOMPLETE")
        self.assertEqual(any("Configured owners differ" in error for error in value["errors"]), True)

    def test_failed_expected_analysis_preserves_native_exit(self) -> None:
        invocation = self.fixture()
        analysis = invocation / "expected-analysis.jsonl"
        analysis.write_text('{"id":{"buildFinished":{}},"finished":{"overallSuccess":false}}\n')
        self.write_events(invocation, [])
        (invocation / "native-exit").write_text("1\n")
        self.capture(invocation)
        value = self.verdict(invocation, analysis=analysis)
        self.assertEqual((value["status"], value["native_exit"]), ("FAIL", 1))
        self.assertEqual(any("Expected analysis:" in error for error in value["errors"]), True)
        combined = report.bundle(self.evidence, 1, 0, self.root / "bundle.tar.gz")
        self.assertEqual((combined["status"], combined["native_exit"]), ("FAIL", 1))

    def test_loading_failure_reaches_receipt_without_provider_secrets(self) -> None:
        invocation = self.fixture()
        analysis = invocation / "expected-analysis.jsonl"
        description = (
            'Repository fetch failed: Get "https://user:password@proxy.example/module?token=secret": '
            "net/http: TLS handshake timeout\nAuthorization: Bearer private-token"
        )
        analysis.write_text("".join(json.dumps(event) + "\n" for event in [
            {"id": {"targetCompleted": {"label": "//example:no_build", "configuration": {"id": "config"}}},
             "aborted": {"reason": "NO_BUILD"}},
            {"id": {"targetConfigured": {"label": "//example:dependent"}},
             "aborted": {"reason": "ANALYSIS_FAILURE"}},
            {"id": {"unconfiguredLabel": {"label": "@@repository//:package"}},
             "aborted": {"reason": "LOADING_FAILURE", "description": description}},
            {"id": {"targetConfigured": {"label": "//example:test"}},
             "aborted": {"reason": "ANALYSIS_FAILURE"}},
            {"id": {"buildFinished": {}}, "finished": {"exitCode": {"name": "BUILD_FAILURE", "code": 1}}},
        ]))
        self.write_events(invocation, [])
        (invocation / "native-exit").write_text("1\n")
        self.capture(invocation)
        value = self.verdict(invocation, analysis=analysis)
        self.assertEqual((value["status"], value["native_exit"], value["executed"]), ("FAIL", 1, []))
        artifact = self.root / "bundle.tar.gz"
        combined = report.bundle(self.evidence, 1, 0, artifact)
        receipt = report.receipt(artifact, report.file_record(artifact)["sha256"], combined["sources"][0])
        self.assertEqual((receipt["status"], receipt["executed_attempt_count"]), ("FAIL", 0))
        error, = [error for error in receipt["invocations"][0]["errors"] if "Bazel analysis aborted" in error]
        self.assertIn("LOADING_FAILURE", error)
        self.assertEqual(error.count("ANALYSIS_FAILURE"), 1)
        self.assertIn("reported net/http: TLS handshake timeout", error)
        self.assertIn(hashlib.sha256(description.encode()).hexdigest(), error)
        for secret in ("password", "proxy.example", "token=secret", "Authorization", "private-token"):
            self.assertNotIn(secret, json.dumps(receipt))

    def test_analysis_abort_keeps_unknown_description_private(self) -> None:
        analysis = self.root / "analysis.jsonl"
        for reason in ("ANALYSIS_FAILURE", "private-provider-reason", ["private-provider-reason"]):
            with self.subTest(reason=reason):
                description = "private description with a password"
                analysis.write_text(json.dumps({
                    "id": {"unconfiguredLabel": {"label": "private-label"}},
                    "aborted": {"reason": reason, "description": description},
                }) + "\n")
                value = report.preflight(analysis, 1, "test")
                self.assertEqual(value["status"], "INCOMPLETE")
                error, = value["errors"]
                self.assertIn("ANALYSIS_FAILURE" if reason == "ANALYSIS_FAILURE" else "UNKNOWN", error)
                self.assertIn(hashlib.sha256(description.encode()).hexdigest(), error)
                for secret in ("private-label", "private description", "private-provider-reason", "password"):
                    self.assertNotIn(secret, error)

    def test_build_uses_independent_expected_configuration(self) -> None:
        invocation = self.fixture()
        events = [event for event in self.events(invocation) if "configured" in event or "finished" in event]
        configured = next(event for event in events if "configured" in event)
        second = json.loads(json.dumps(configured))
        old_label = configured["id"]["targetConfigured"]["label"]
        second = json.loads(json.dumps(second).replace(old_label, "//another:binary"))
        second["configured"].pop("testSize")
        second["configured"]["targetKind"] = "go_binary rule"
        analysis = invocation / "expected-analysis.jsonl"
        analysis.write_text("".join(json.dumps(event) + "\n" for event in events + [second]))
        context = report.read_json(invocation / "context.json")
        context["command_kind"] = "build"
        report.write_json(invocation / "context.json", context)
        (invocation / "executed-arguments.txt").write_text("build\n//...\n")
        (invocation / "expected.txt").write_text("")
        self.write_events(invocation, [event for event in self.events(invocation)
                                       if "testResult" not in event and "testSummary" not in event])
        self.capture(invocation)
        value = self.verdict(invocation, analysis=analysis)
        self.assertEqual(value["status"], "INCOMPLETE")
        self.assertEqual(any(item["label"] == "//another:binary" for item in value["missing"]), True)

    def aspect_fixture(self) -> Path:
        invocation = self.fixture()
        events = [json.loads(line) for line in (FIXTURES / "aspect-analysis/events.jsonl").read_text().splitlines()]
        for event in events:
            if "aborted" in event:
                del event["aborted"]
                event["completed"] = {"success": True}
        self.write_events(invocation, events)
        context = report.read_json(invocation / "context.json")
        context["command_kind"] = "build"
        report.write_json(invocation / "context.json", context)
        (invocation / "executed-arguments.txt").write_text("build\n--config=lint-cc\n")
        (invocation / "expected.txt").write_text("//test/syscalls/linux:tuntap_test\n")
        return invocation

    def test_aspect_analysis_selects_only_the_ordinary_root(self) -> None:
        analysis = FIXTURES / "aspect-analysis/events.jsonl"
        value = report.preflight(analysis, 0, "build")
        self.assertEqual(value["status"], "PASS", value)
        self.assertEqual([owner["label"] for owner in value["expected"]], ["//test/syscalls/linux:tuntap_test"])
        # Ordinary roots in multiple configurations are still ambiguous.
        events = [json.loads(line) for line in analysis.read_text().splitlines()]
        ordinary = next(event for event in events if "configured" in event)
        extra = json.loads(json.dumps(ordinary))
        extra["children"][0]["targetCompleted"]["configuration"]["id"] = "another-configuration"
        path = self.root / "ambiguous.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events + [extra]))
        self.assertEqual(report.preflight(path, 0, "build")["status"], "INCOMPLETE")

    def test_preflight_rejects_orphaned_aspect_identity(self) -> None:
        original = [json.loads(line) for line in (FIXTURES / "aspect-analysis/events.jsonl").read_text().splitlines()]
        for mismatch in ("label", "configuration"):
            with self.subTest(mismatch=mismatch):
                events = json.loads(json.dumps(original))
                aspect = next(event for event in events if event.get("id", {}).get("targetConfigured", {}).get("aspect"))
                if mismatch == "label":
                    aspect["id"]["targetConfigured"]["label"] = "//orphan:target"
                    aspect["children"][0]["targetCompleted"]["label"] = "//orphan:target"
                else:
                    aspect["children"][0]["targetCompleted"]["configuration"]["id"] = "another-configuration"
                path = self.root / "orphaned-aspect.jsonl"
                path.write_text("".join(json.dumps(event) + "\n" for event in events))
                value = report.preflight(path, 0, "build")
                self.assertEqual(value["status"], "INCOMPLETE", value)
                self.assertIn("no matching ordinary root", value["errors"][0])

    def test_aspect_abort_does_not_replace_the_ordinary_root(self) -> None:
        original = [json.loads(line) for line in (FIXTURES / "aspect-analysis/events.jsonl").read_text().splitlines()]
        for missing_root in (False, True):
            with self.subTest(missing_root=missing_root):
                events = json.loads(json.dumps(original))
                for event in events:
                    if "configured" in event and not event["id"]["targetConfigured"].get("aspect"):
                        event["configured"]["testSize"] = "SMALL"
                    if "finished" in event:
                        event["finished"]["overallSuccess"] = False
                        event["finished"]["exitCode"] = {"name": "BUILD_FAILURE", "code": 1}
                    if not missing_root and event.get("id", {}).get("targetCompleted", {}).get("aspect"):
                        event["aborted"]["reason"] = "SKIPPED"
                if missing_root:
                    events = [event for event in events if not ("aborted" in event and not event["id"]["targetCompleted"].get("aspect"))]
                path = self.root / "aspect-abort.jsonl"
                path.write_text("".join(json.dumps(event) + "\n" for event in events))
                value = report.preflight(path, 1, "test")
                self.assertEqual(value["status"], "INCOMPLETE" if missing_root else "PASS", value)
                if not missing_root:
                    self.assertEqual([owner["label"] for owner in value["expected"]], ["//test/syscalls/linux:tuntap_test"])

    def test_aspect_completions_are_independent_of_target_completions(self) -> None:
        invocation = self.aspect_fixture()
        original = self.events(invocation)
        analysis = FIXTURES / "aspect-analysis/events.jsonl"
        for failed in (None, "aspect", "target"):
            for reverse in (False, True):
                with self.subTest(failed=failed, reverse=reverse):
                    events = json.loads(json.dumps(original))
                    for event in events:
                        if "completed" in event:
                            kind = "aspect" if event["id"]["targetCompleted"].get("aspect") else "target"
                            event["completed"]["success"] = kind != failed
                        if "finished" in event and failed:
                            event["finished"]["overallSuccess"] = False
                            event["finished"]["exitCode"] = {"name": "BUILD_FAILURE", "code": 1}
                    if reverse:
                        events.reverse()
                    self.write_events(invocation, events)
                    (invocation / "native-exit").write_text("1\n" if failed else "0\n")
                    self.assertEqual(self.capture(invocation)["errors"], [])
                    value = self.verdict(invocation, analysis=analysis)
                    self.assertEqual(value["status"], "FAIL" if failed else "PASS", value)
                    self.assertEqual(value["missing"], [])
                    self.assertEqual([row["reason"] for row in value["failures"]], [failed + " build failed"] if failed else [])
                    self.assertEqual(value["completed_aspects"], [{**value["expected_aspects"][0], "success": failed != "aspect"}])
                    if failed is None and not reverse:
                        archive = self.root / "aspects.tar.gz"
                        report.bundle(self.evidence, 0, 0, archive)
                        receipt = report.receipt(archive, report.file_record(archive)["sha256"], value["source"])
                        for field in ("expected_aspects", "completed_aspects"):
                            self.assertEqual(receipt["invocations"][0][field], value[field])

    def test_missing_or_wrong_aspect_completion_is_incomplete(self) -> None:
        invocation = self.aspect_fixture()
        original = self.events(invocation)
        for mutation in ("missing-completion", "wrong-configuration", "missing-aspect"):
            with self.subTest(mutation=mutation):
                events = json.loads(json.dumps(original))
                aspect = next(event for event in events if "completed" in event and event["id"]["targetCompleted"].get("aspect"))
                if mutation == "wrong-configuration":
                    aspect["id"]["targetCompleted"]["configuration"]["id"] = "another-configuration"
                else:
                    events.remove(aspect)
                if mutation == "missing-aspect":
                    events = [event for event in events if not event.get("id", {}).get("targetConfigured", {}).get("aspect")]
                self.write_events(invocation, events)
                self.capture(invocation)
                value = self.verdict(invocation, analysis=FIXTURES / "aspect-analysis/events.jsonl")
                self.assertEqual(value["status"], "INCOMPLETE", value)
                self.assertEqual([row["reason"] for row in value["missing"]], ["aspect completion missing"])

    def test_analysis_only_does_not_require_aspect_execution(self) -> None:
        invocation = self.aspect_fixture()
        (invocation / "events.jsonl").write_bytes((FIXTURES / "aspect-analysis/events.jsonl").read_bytes())
        context = report.read_json(invocation / "context.json")
        context["command_kind"] = "analysis"
        report.write_json(invocation / "context.json", context)
        (invocation / "executed-arguments.txt").write_text("build\n--config=lint-cc\n--nobuild\n")
        self.capture(invocation)
        value = self.verdict(invocation)
        self.assertEqual(value["status"], "PASS", value)
        self.assertEqual(value["missing"], [])
        self.assertTrue(value["accepted_analysis_exit"])

    def test_execution_without_expected_selection_is_incomplete(self) -> None:
        invocation = self.fixture()
        self.capture(invocation)
        value = report.verdict(invocation / "captured.jsonl", report.manifest_path(invocation / "captured.jsonl"),
                               invocation / "context.json")
        self.assertEqual(value["status"], "INCOMPLETE")
        self.assertIn("No independent expected selection was retained", value["errors"])

    def test_missing_announced_result(self) -> None:
        invocation = self.fixture()
        self.write_events(invocation, [event for event in self.events(invocation) if "testResult" not in event])
        self.capture(invocation)
        value = self.verdict(invocation)
        self.assertEqual(value["status"], "INCOMPLETE")
        self.assertEqual(any(item.get("reason") == "announced result missing" for item in value["missing"]), True)

    def test_summary_shard_dimension_cannot_hide_a_missing_shard(self) -> None:
        invocation = self.fixture()
        events = self.events(invocation)
        for event in events:
            if "testSummary" in event:
                event["testSummary"]["shardCount"] = 2
        self.write_events(invocation, events)
        self.capture(invocation)
        value = self.verdict(invocation)
        self.assertEqual(value["status"], "INCOMPLETE")
        self.assertEqual(any("run/shard coverage" in error for error in value["errors"]), True)

    def test_source_identity_and_clean_snapshots(self) -> None:
        for field, body in (("commit.txt", "changed commit"), ("source-after.txt", " M source.go\n"),
                            ("final-head.txt", "0" * 40 + "\n")):
            with self.subTest(field=field):
                invocation = self.fixture()
                (invocation / field).write_text(body)
                self.capture(invocation)
                self.assertEqual(self.verdict(invocation)["status"], "INCOMPLETE")
                shutil.rmtree(invocation)

    def test_native_failure_stays_primary_when_capture_fails(self) -> None:
        invocation = self.fixture()
        (invocation / "test.log").unlink()
        (invocation / "native-exit").write_text("37\n")
        (invocation / "capture-exit").write_text("1\n")
        self.assertEqual(len(self.capture(invocation)["reports"]), 1)
        value = self.verdict(invocation)
        self.assertEqual((value["status"], value["native_exit"]), ("FAIL", 37))
        combined = report.bundle(self.evidence, 37, 1, self.root / "evidence.tar.gz")
        self.assertEqual((combined["status"], combined["native_exit"]), ("FAIL", 37))

    def test_bundle_retains_failure_diagnostics_and_declared_results(self) -> None:
        for status, tag, name in itertools.product(
                ("PASSED", "TIMEOUT"), (None, "source-image-results"),
                ("test.outputs__outputs.zip", "test.outputs/guest/console.log",
                 "test.outputs/source-images/selection.json", "test.outputs/source-images/basic_alpine/request.json",
                 "test.outputs/source-images/basic_alpine/build-exit-code", "test.outputs/source-images/basic_alpine/inspect.json",
                 "test.outputs/source-images/basic_alpine/exit-code")):
            with self.subTest(status=status, tag=tag, name=name):
                invocation = self.fixture()
                relative = "test.outputs/outputs.zip" if name.endswith(".zip") else name
                outputs = invocation / relative
                outputs.parent.mkdir(parents=True)
                if name.endswith(".zip"):
                    with zipfile.ZipFile(outputs, "w") as archive:
                        archive.writestr("guest.log", "payload completed before wrapper timeout\n")
                else:
                    outputs.write_text("payload completed before wrapper timeout\n")
                original = outputs.read_bytes()
                events = self.events(invocation)
                if tag:
                    configured = next(event["configured"] for event in events if "configured" in event)
                    configured.setdefault("tag", []).append(tag)
                    # Results can precede their configured owner in BEP.
                    events.reverse()
                result = next(event["testResult"] for event in events if "testResult" in event)
                result["status"] = status
                result["testActionOutput"].append({"name": name, "uri": outputs.as_uri()})
                self.write_events(invocation, events)
                native_exit = 0 if status == "PASSED" else 3
                (invocation / "native-exit").write_text(f"{native_exit}\n")
                self.assertEqual(self.capture(invocation)["errors"], [])
                # Resume must retain verified diagnostics even after the
                # original worker outputs have disappeared.
                outputs.unlink()
                self.assertEqual(self.capture(invocation)["errors"], [])
                self.assertEqual(self.verdict(invocation)["status"], "PASS" if status == "PASSED" else "FAIL")
                bundle = self.root / "evidence.tar.gz"
                report.bundle(self.evidence, native_exit, 0, bundle)
                with tarfile.open(bundle) as archive:
                    path = f"invocation-pass/reports/{relative}"
                    if status == "PASSED" and (tag is None or name == "test.outputs/guest/console.log"):
                        self.assertNotIn(path, archive.getnames())
                    else:
                        with archive.extractfile(path) as stream:
                            self.assertEqual(stream.read(), original)
                shutil.rmtree(invocation)

    def test_missing_diagnostics_preserve_test_failure_and_other_reports(self) -> None:
        invocation = self.fixture()
        events = self.events(invocation)
        result = next(event["testResult"] for event in events if "testResult" in event)
        result["status"] = "TIMEOUT"
        result["testActionOutput"].append({"name": "test.outputs__outputs.zip", "uri": (invocation / "missing.zip").as_uri()})
        self.write_events(invocation, events)
        (invocation / "native-exit").write_text("3\n")
        (invocation / "capture-exit").write_text("1\n")
        captured = self.capture(invocation)
        self.assertEqual(len(captured["errors"]), 1)
        self.assertEqual([row["name"] for row in captured["reports"]], ["test.log", "test.xml"])
        value = self.verdict(invocation)
        self.assertEqual((value["status"], value["native_exit"]), ("FAIL", 3))
        combined = report.bundle(self.evidence, 3, 1, self.root / "evidence.tar.gz")
        self.assertEqual((combined["status"], combined["native_exit"]), ("FAIL", 3))

    @unittest.skipUnless(sys.platform == "linux", "The Actions entry point requires Linux")
    def test_work_deadline_reaches_capture_and_bundle(self) -> None:
        # Run the actual shell entry point and reporter. Only Git identity and
        # Bazel are fixtures; the real timeout interrupts a waiting child.
        bazel_options = ["--config=plugin-tldk", "--define=literal=before#after with spaces"]
        cases = (
            ("preflight", "amd64", None),
            ("execution", "amd64", "parallel/test-http-outgoing-end-cork.js"),
            ("execution", "amd64", ""),
            ("execution", "amd64", None),
            ("execution", "all", "parallel/test-http-outgoing-end-cork.js"),
            ("execution", "all", ""),
            ("execution", "all", None),
            ("image-selection", "arm64", None),
            ("docker-selection", "arm64", None),
            ("command-pass", "amd64", None),
            ("command-fail", "amd64", None),
            ("command-timeout", "amd64", None),
        )
        for index, (phase, architecture, runtime_filter) in enumerate(cases):
            with self.subTest(phase=phase, architecture=architecture, runtime_filter=runtime_filter):
                workspace = self.root / str(index)
                scripts = workspace / "test/rbe"
                scripts.mkdir(parents=True)
                for name in (
                    "actions.sh",
                    "license.bazelrc",
                    "qualification.bazelrc",
                    "qualify.sh",
                    "qualification_report.py",
                    "unit_matrix.py",
                ):
                    shutil.copy2(Path(__file__).with_name(name), scripts / name)
                (workspace / "test/benchmarks").mkdir()
                shutil.copyfile(LIFECYCLE_MANIFEST, workspace / "test/benchmarks/lifecycle.json")
                (workspace / "MODULE.bazel.lock").write_text("fixture lock\n")
                binary = workspace / "bin"
                binary.mkdir()
                (binary / "python3").symlink_to(sys.executable)
                git = binary / "git"
                git.write_text("""#!/usr/bin/env python3
import os
from pathlib import Path
import sys
args = sys.argv[1:]
if args[:1] == ['-C']:
    args = args[2:]
if args == ['rev-parse', 'HEAD']:
    print(os.environ['TEST_HEAD'])
elif args == ['cat-file', '-p', 'HEAD']:
    sys.stdout.write(Path(os.environ['TEST_COMMIT_FILE']).read_text())
elif args[:1] not in (['status'], ['diff']):
    raise SystemExit('Unexpected fixture Git command: ' + repr(args))
""")
                git.chmod(0o755)
                bazel = binary / "bazel"
                bazel.write_text("""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import signal
import sys
with Path(os.environ['TEST_BAZEL_ARGUMENTS']).open('a') as stream:
    stream.write(json.dumps({
        'arguments': sys.argv[1:],
        'runtime_filter': os.environ.get('RUNTIME_TESTS_FILTER'),
    }) + '\\n')
if os.environ['TEST_PHASE'].startswith('command-'):
    Path(os.environ['TEST_WORK_STARTED']).touch()
    if os.environ['TEST_PHASE'] == 'command-timeout':
        signal.pause()
    if 'query' in sys.argv:
        sys.stdout.buffer.write(b'<query> spaces # and\\nnewlines </query>')
        sys.stderr.write('query diagnostic\\n')
    elif '//:gazelle_check' in sys.argv:
        sys.stdout.write('gazelle check output\\n')
    else:
        source = Path(sys.argv[sys.argv.index('--input') + 1])
        assert source.read_bytes() == b'<query> spaces # and\\nnewlines </query>'
        raise SystemExit(23 if os.environ['TEST_PHASE'] == 'command-fail' else 0)
    raise SystemExit(0)
events = Path(next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--build_event_json_file=')))
events.write_text(Path(os.environ['TEST_ANALYSIS']).read_text())
if 'aquery' in sys.argv:
    analysis = [json.loads(line) for line in events.read_text().splitlines()]
    for event in analysis:
        event.pop('aborted', None)
        if 'finished' in event:
            event['finished']['exitCode'] = {'name': 'SUCCESS', 'code': 0}
            event['finished']['overallSuccess'] = True
    events.write_text(''.join(json.dumps(event) + '\\n' for event in analysis))
    raise SystemExit(0)
if '--nobuild' in sys.argv:
    raise SystemExit(1)
Path(os.environ['TEST_WORK_STARTED']).touch()
signal.pause()
""")
                bazel.chmod(0o755)
                runner_temp = workspace / "temporary"
                runner_temp.mkdir()
                started = workspace / "work-started"
                analysis = [json.loads(line) for line in (FIXTURES / "analysis-no-build/events.jsonl").read_text().splitlines()]
                configured = next(event for event in analysis if "configured" in event)
                configured["configured"].setdefault("tag", []).append("jtreg-results")
                tagged = configured["id"]["targetConfigured"]["label"]
                lane = "test"
                target = "//test/syscalls:exec_binary_test_native_amd64"
                if phase.endswith("-selection"):
                    lane, target = {
                        "image-selection": ("cpu-images", "//test/rbe:gcsfuse_arm64_test"),
                        "docker-selection": ("docker", "//test/e2e:uds_proxy_test_owned"),
                    }[phase]
                    analysis = json.loads(json.dumps(analysis).replace(tagged, target))
                elif phase.startswith("command-"):
                    lane = "gazelle"
                analysis_path = workspace / "analysis.jsonl"
                analysis_path.write_text("".join(json.dumps(event) + "\n" for event in analysis))
                source = (FIXTURES / "pass/final-head.txt").read_text().strip()
                environment = {
                    **os.environ,
                    "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                    "PYTHONPATH": str(scripts),
                    "RUNNER_TEMP": str(runner_temp),
                    "BUILDBUDDY_ARTIFACTS_DIRECTORY": str(runner_temp),
                    "QUALIFICATION_COMMIT": source,
                    "QUALIFICATION_EXECUTION": "remote",
                    "QUALIFICATION_ARCH": architecture,
                    "QUALIFICATION_LANES": lane,
                    "QUALIFICATION_TARGETS": "" if phase.startswith("command-") else target,
                    "QUALIFICATION_HEADER_BASE": "",
                    "QUALIFICATION_BENCHMARK_MANIFEST": "",
                    "QUALIFICATION_COS_GZIP_BASE64": "",
                    "QUALIFICATION_COS_SHA256": "",
                    "QUALIFICATION_WORK_DEADLINE": str(time.monotonic() + (
                        -1 if phase == "preflight" else 30 if phase in ("command-pass", "command-fail") else 5
                    )),
                    "TEST_PHASE": phase,
                    "TEST_HEAD": source,
                    "TEST_COMMIT_FILE": str(FIXTURES / "pass/commit.txt"),
                    "TEST_ANALYSIS": str(analysis_path),
                    "TEST_WORK_STARTED": str(started),
                    "TEST_BAZEL_ARGUMENTS": str(workspace / "bazel-arguments.jsonl"),
                }
                environment.pop("RUNTIME_TESTS_FILTER", None)
                if runtime_filter is not None:
                    environment["RUNTIME_TESTS_FILTER"] = runtime_filter
                options = [f"--bazel-option={option}" for option in bazel_options] if lane == "test" else []
                result = subprocess.run(
                    ["bash", "test/rbe/actions.sh", *options],
                    cwd=workspace, env=environment, capture_output=True, text=True, timeout=45, check=False,
                )
                self.assertEqual(result.returncode == 0, phase == "command-pass", result.stdout + result.stderr)
                self.assertEqual(started.exists(), phase != "preflight", result.stdout + result.stderr)
                if phase.endswith("-selection"):
                    observations = [json.loads(line) for line in (workspace / "bazel-arguments.jsonl").read_text().splitlines()]
                    commands = [row["arguments"] for row in observations if "test" in row["arguments"]]
                    self.assertEqual(len(commands), 2, commands)
                    for command in commands:
                        self.assertEqual([arg for arg in command if arg.startswith("//")], [target])
                if phase == "execution":
                    observations = [json.loads(line) for line in (workspace / "bazel-arguments.jsonl").read_text().splitlines()]
                    profiles = [row["arguments"] for row in observations if "aquery" in row["arguments"]]
                    self.assertEqual(len(profiles), 2 if architecture == "all" else 0, observations)
                    tests = [row for row in observations if "test" in row["arguments"]]
                    commands = [row["arguments"] for row in tests]
                    self.assertEqual(len(commands), 2, commands)
                    for row in tests:
                        self.assertEqual(row["runtime_filter"], runtime_filter)
                        self.assertEqual("--test_env=RUNTIME_TESTS_FILTER" in row["arguments"],
                                         runtime_filter is not None)
                        self.assertEqual("--config=rbe-matrix" in row["arguments"], architecture == "all")
                    for command in commands:
                        for option in bazel_options:
                            self.assertIn(option, command)
                    self.assertFalse(any(option.startswith("--remote_download_regex=") for option in commands[0]))
                    self.assertIn("--nozip_undeclared_test_outputs", commands[1])
                    [pattern] = [option.removeprefix("--remote_download_regex=") for option in commands[1]
                                 if option.startswith("--remote_download_regex=")]
                    path = "bazel-out/config/testlogs/" + tagged.removeprefix("//").replace(":", "/")
                    self.assertRegex(path + "/run_1_of_2/test.outputs/batch-0.log", pattern)
                    self.assertRegex(path + "/test.outputs/batch-0-sentry-stacks.log", pattern)
                    self.assertIsNone(re.search(pattern, path + "/test.outputs/boot.log"))
                    self.assertIsNone(re.search(pattern, path + "_other/test.outputs/batch-0.log"))
                    [arguments] = (runner_temp / "qualification").glob("invocation-*/arguments.txt")
                    for option in ("--nozip_undeclared_test_outputs", "--remote_download_regex=" + pattern):
                        self.assertIn(option, arguments.read_text().splitlines())
                artifact = runner_temp / "qualification.tar.gz"
                self.assertTrue(artifact.is_file(), result.stdout + result.stderr)
                value = report.read_json(runner_temp / "qualification/verdict.json")
                expected_exit = 0 if phase == "command-pass" else 23 if phase == "command-fail" else 124
                self.assertEqual((value["status"], value["native_exit"]), ("PASS" if expected_exit == 0 else "FAIL", expected_exit), value)
                self.assertEqual(value["runner_exit"], result.returncode, value)
                if phase in ("command-pass", "command-fail"):
                    self.assertEqual([row["bazel_command"] for row in value["commands"]].count("run"), 2)
                    self.assertEqual([row["bazel_command"] for row in value["commands"]].count("query"), 1)
                    self.assertEqual(result.stdout.count("gazelle check output\n"), 1)
                    self.assertEqual(result.stderr.count("query diagnostic\n"), 1)
                    self.assertNotIn("<query>", result.stdout)
                    self.assertEqual(value["expected"], [])
                    self.assertEqual(value["executed"], [])
                with tarfile.open(artifact) as archive:
                    self.assertIn("verdict.json", archive.getnames())

    def test_capture_resumes_after_independent_copy_failure(self) -> None:
        invocation = self.fixture()
        log = (invocation / "test.log").read_bytes()
        (invocation / "test.log").unlink()
        first = self.capture(invocation)
        self.assertEqual(len(first["errors"]), 1)
        self.assertEqual([row["name"] for row in first["reports"]], ["test.xml"])
        (invocation / "test.log").write_bytes(log)
        # The source XML can be gone: the previous verified copy is sufficient.
        (invocation / "test.xml").unlink()
        repaired = self.capture(invocation)
        self.assertEqual(repaired["errors"], [])
        self.assertEqual(self.verdict(invocation)["status"], "PASS")

    def test_resume_repairs_only_missing_or_corrupt_copies(self) -> None:
        invocation = self.fixture()
        self.capture(invocation)
        originals = {name: (invocation / name).read_bytes() for name in ("test.log", "test.xml")}
        for name in originals:
            (invocation / name).unlink()
        self.assertEqual(self.capture(invocation)["errors"], [])
        for name, contents in originals.items():
            (invocation / name).write_bytes(contents)
        (invocation / "reports/test.log").write_text("corrupt")
        (invocation / "reports/test.xml").unlink()
        repaired = self.capture(invocation)
        self.assertEqual(repaired["errors"], [])
        self.assertEqual(self.verdict(invocation)["status"], "PASS")

    def test_changed_bep_cannot_reuse_old_copies(self) -> None:
        invocation = self.fixture()
        self.capture(invocation)
        (invocation / "test.xml").unlink()
        with (invocation / "events.jsonl").open("a") as events:
            events.write('{"id": {"progress": {"opaqueCount": 99}}}\n')
        value = self.capture(invocation)
        self.assertEqual(len(value["errors"]), 1)
        self.assertEqual([row["name"] for row in value["reports"]], ["test.log"])

    def test_capture_rejects_escape_but_keeps_other_report(self) -> None:
        invocation = self.fixture()
        events = self.events(invocation)
        for event in events:
            if "testResult" in event:
                event["testResult"]["testActionOutput"][0]["uri"] = (self.root / "outside.log").as_uri()
        self.write_events(invocation, events)
        value = self.capture(invocation)
        self.assertEqual(len(value["errors"]), 1)
        self.assertEqual([row["name"] for row in value["reports"]], ["test.xml"])

    def test_all_skipped_is_not_executed_coverage(self) -> None:
        invocation = self.fixture()
        (invocation / "test.xml").write_text('<testsuite tests="1"><testcase name="unavailable"><skipped>environment</skipped></testcase></testsuite>')
        self.capture(invocation)
        value = self.verdict(invocation)
        self.assertEqual(value["status"], "INCOMPLETE")
        self.assertEqual(value["case_counts"]["skipped"], 1)
        self.assertEqual(len(value["all_skipped_owners"]), 1)
        self.assertEqual(value["zero_case_owners"], [])
        self.assertEqual(value["skips"][0]["reason"], "environment")

    def test_each_owner_requires_executed_cases(self) -> None:
        for xml, field, skips in (
            ("<testsuites/>", "zero_case_owners", 4),
            ('<testsuite><testcase name="unavailable"><skipped>environment</skipped></testcase></testsuite>',
             "all_skipped_owners", 5),
        ):
            with self.subTest(field=field):
                invocation = self.mixed_fixture(xml)
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation, analysis=self.analysis(invocation))
                self.assertEqual((value["status"], value["native_exit"]), ("INCOMPLETE", 0))
                self.assertEqual(value["case_counts"], {"passed": 45, "failed": 0, "skipped": skips})
                self.assertEqual([owner["label"] for owner in value[field]], ["//another:test"])
                self.assertEqual(value["missing"], [])
                self.assertEqual(len(value["errors"]), 1, value)
                self.assertIn("//another:test", value["errors"][0])
                shutil.rmtree(invocation)

    def test_empty_owner_does_not_hide_native_or_xml_failure(self) -> None:
        for failure in ("native", "xml"):
            with self.subTest(failure=failure):
                invocation = self.mixed_fixture("<testsuites/>")
                if failure == "native":
                    (invocation / "native-exit").write_text("3\n")
                else:
                    (invocation / "test.xml").write_text(
                        '<testsuite><testcase name="passed"/><testcase name="failed"><failure>original failure</failure></testcase></testsuite>')
                self.assertEqual(self.capture(invocation)["errors"], [])
                value = self.verdict(invocation)
                self.assertEqual(value["status"], "FAIL", value)
                self.assertEqual([owner["label"] for owner in value["zero_case_owners"]], ["//another:test"])
                self.assertEqual(value["native_exit"], 3 if failure == "native" else 0)
                if failure == "xml":
                    self.assertEqual(value["case_counts"]["failed"], 1)
                shutil.rmtree(invocation)

    def test_empty_shard_preserves_same_owner_executed_coverage(self) -> None:
        invocation = self.fixture()
        events = self.events(invocation)
        result = next(event for event in events if "testResult" in event)
        empty = json.loads(json.dumps(result))
        empty["id"]["testResult"]["shard"] = 2
        empty_xml = invocation / "empty.xml"
        empty_xml.write_text("<testsuites/>")
        for output in empty["testResult"]["testActionOutput"]:
            if output["name"] == "test.xml":
                output["uri"] = empty_xml.as_uri()
        for event in events:
            if "completed" in event:
                event["children"].append(empty["id"])
            if "testSummary" in event:
                event["testSummary"].update(shardCount=2, totalRunCount=2)
        self.write_events(invocation, [empty] + events)
        self.assertEqual(self.capture(invocation)["errors"], [])
        manifest = invocation / "required.json"
        label = (invocation / "expected.txt").read_text().strip()
        report.write_json(manifest, {label: ["ElfTest/Execute"]})
        value = self.verdict(invocation, case_manifest=manifest)
        self.assertEqual(value["status"], "PASS", value)
        self.assertEqual(value["case_counts"], {"passed": 45, "failed": 0, "skipped": 4})
        self.assertEqual(value["zero_case_owners"], [])
        self.assertEqual(value["all_skipped_owners"], [])
        self.assertEqual(len(value["executed"]), 2)

    def test_xml_notrun_and_wrapper_error(self) -> None:
        path = self.root / "report.xml"
        path.write_text('<testsuite tests="2"><testcase name="notrun" status="notrun"/><testcase name="ran"/></testsuite>')
        value = report.xml_cases(path)
        self.assertEqual((value["passed"], value["skipped"]), (1, 1))
        path.write_text('<testsuite tests="0" failures="1"/>')
        with self.assertRaisesRegex(ValueError, "failures without"):
            report.xml_cases(path)

    def test_analysis_is_distinct_from_test_execution(self) -> None:
        invocation = self.fixture()
        self.write_events(invocation, [event for event in self.events(invocation)
                                       if "testResult" not in event and "testSummary" not in event])
        context = report.read_json(invocation / "context.json")
        context["command_kind"] = "analysis"
        report.write_json(invocation / "context.json", context)
        (invocation / "executed-arguments.txt").write_text('--bazelrc=/private.rc\ntest\n--nobuild\n')
        self.capture(invocation)
        self.assertEqual(self.verdict(invocation)["status"], "PASS")
        archive = self.root / "analysis.tar.gz"
        report.bundle(self.evidence, 0, 0, archive)
        receipt = report.receipt(archive, report.file_record(archive)["sha256"], context["expected_source"])
        [phase] = receipt["invocations"]
        self.assertEqual((phase["expected_owner_count"], phase["executed_attempt_count"]), (1, 0))
        context["command_kind"] = "test"
        report.write_json(invocation / "context.json", context)
        self.assertEqual(self.verdict(invocation)["status"], "INCOMPLETE")

    def test_bundle_requires_every_invocation_and_excludes_aggregate(self) -> None:
        invocation = self.fixture()
        self.capture(invocation)
        self.verdict(invocation)
        output = self.root / "bundle.tar.gz"
        value = report.bundle(self.evidence, 0, 0, output)
        self.assertEqual(value["status"], "PASS")
        self.assertEqual(len(value["invocations"]), 1)
        with tarfile.open(output) as archive:
            self.assertIn("verdict.json", archive.getnames())
            self.assertIn("invocation-pass/reports/test.xml", archive.getnames())
        self.assertEqual(report.bundle(self.evidence, 0, 0, output)["status"], "PASS")
        (self.evidence / "invocation-missing").mkdir()
        self.assertEqual(report.bundle(self.evidence, 0, 0, output)["status"], "INCOMPLETE")

    def test_go_case_preparation_failure(self) -> None:
        invocation = self.evidence / "invocation-aborted"
        invocation.mkdir()
        source = report.read_json(FIXTURES / "pass" / "context.json")["expected_source"]
        shutil.copyfile(FIXTURES / "pass" / "commit.txt", invocation / "commit.txt")
        labels = ["//pkg/aio:aio_test_amd64", "//pkg/aio:aio_test_arm64"]
        (invocation / "arguments.txt").write_text("test\n--config=rbe-matrix\n--target_pattern_file=/tmp/targets\n")
        (invocation / "target-patterns.txt").write_text("\n".join(labels) + "\n")
        for name in ("source-before.txt", "source-after.txt", "go-case-stdout.log"):
            (invocation / name).write_text("")
        (invocation / "final-head.txt").write_text(source + "\n")
        (invocation / "go-case-options.txt").write_text("--config=rbe-matrix\n")
        (invocation / "go-case-exit.txt").write_text("1\n")
        diagnostic = "Expected a case-name array for //pkg/aio:aio_test_amd64\n"
        (invocation / "go-case-stderr.log").write_text(diagnostic)
        original_input = json.dumps({labels[0]: "NotAnArray"}) + "\n"
        (self.evidence / "go-cases.json").write_text(original_input)
        checksum = hashlib.sha256(original_input.encode()).hexdigest()
        (self.evidence / "go-cases.sha256").write_text(checksum + "  /tmp/go-cases.json\n")
        record = {"source": source, "target_pattern_files": ["/tmp/targets"]}
        report.write_json(invocation / "preparation-failure.json", record)
        archive = self.root / "aborted.tar.gz"
        value = report.bundle(self.evidence, 1, 0, archive)
        self.assertEqual((value["status"], value["native_exit"], value["runner_exit"]),
                         ("INCOMPLETE", None, 1))
        self.assertEqual((value["sources"], value["requested_labels"], value["expected"], value["executed"]),
                         ([source], labels, [], []))
        self.assertEqual(value["missing"], [{"label": label,
            "reason": "native execution did not start: Go case analysis failed"} for label in labels])
        preparation, = value["preparation_failures"]
        self.assertEqual((preparation["native_execution_started"], preparation["exit"]), (False, 1))
        receipt = report.receipt(archive, report.file_record(archive)["sha256"], source)
        self.assertEqual((receipt["status"], receipt["invocations"]), ("INCOMPLETE", []))
        with tarfile.open(archive) as retained:
            with retained.extractfile("invocation-aborted/go-case-stderr.log") as stream:
                self.assertEqual(stream.read(), diagnostic.encode())

        for damage, expected in (("input", "pre-run checksum"), ("selection", "differ from test arguments")):
            with self.subTest(damage=damage):
                if damage == "input":
                    (self.evidence / "go-cases.json").write_text("{}\n")
                else:
                    report.write_json(invocation / "preparation-failure.json",
                                      {**record, "target_pattern_files": ["/tmp/other-targets"]})
                damaged = report.bundle(self.evidence, 1, 0, archive)
                self.assertEqual((damaged["status"], damaged["sources"], damaged["native_exit"]),
                                 ("INCOMPLETE", [], None))
                self.assertIn(expected, "\n".join(damaged["errors"]))
                (self.evidence / "go-cases.json").write_text(original_input)
                report.write_json(invocation / "preparation-failure.json", record)

    def test_empty_bundle_never_passes(self) -> None:
        value = report.bundle(self.evidence, 19, 0, self.root / "bundle.tar.gz")
        self.assertEqual(value["status"], "INCOMPLETE")
        self.assertEqual(value["invocations"], [])
        self.assertEqual((value["native_exit"], value["runner_exit"]), (None, 19))

    def test_source_images_accept_configured_empty_mixed_and_build_obligations(self) -> None:
        for scope in ("empty", "mixed", "build"):
            with self.subTest(scope=scope):
                invocation = self.source_image_fixture()
                try:
                    baseline = self.source_image_audit(invocation)
                    image, = baseline["images"]
                    canonical = report.read_json(invocation / "verdict.json")
                    owner, = canonical["expected"]
                    declared = {"name": image["image"], "architecture": "amd64", "archive": image["archive"]["path"]}
                    configured = [{**owner, "images": [declared], "source_commands": []}]
                    owners = {owner["label"]: [declared["name"]]}
                    if scope == "empty":
                        configured[0]["images"] = []
                        owners[owner["label"]] = []
                        # Only the non-image graph projection is applicable to
                        # this source audit; canonical test status is separate.
                        records, _ = report.compact_records(invocation / "execution.json")
                        (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in records.values()))
                    elif scope == "mixed":
                        empty = {"label": "//unit:no_image", "configuration": "other-config"}
                        canonical["expected"].append(empty)
                        canonical["executed"].append({**canonical["executed"][0], "owner": empty})
                        configured.append({**empty, "images": [], "source_commands": []})
                        owners[empty["label"]] = []
                    else:
                        canonical.update(command_kind="build", executed=[])
                    report.write_json(invocation / "verdict.json", canonical)
                    report.write_json(invocation / "expectations.json", {"owners": owners, "configured_owners": configured})
                    value = self.source_image_audit(invocation)
                    self.assertEqual((value["audit_status"], value["canonical_status"]), ("PASS", "PASS"), value)
                    self.assertEqual(len(value["images"]), int(scope != "empty"))
                    self.assertEqual(len(value["consumers"]), int(scope == "mixed"))
                finally:
                    shutil.rmtree(invocation)

    def test_source_images_reject_incomplete_configured_obligations(self) -> None:
        for damage, expected_error in (
                ("missing-owner", "differ from configured owners"),
                ("configuration", "differ from configured owners"),
                ("archive", "archive identities differ"),
                ("build-unconfigured", "Build image audits require"),
                ("empty-unconfigured", "Empty image obligations require"),
                ("dynamic", "Dynamic source-image commands")):
            with self.subTest(damage=damage):
                invocation = self.source_image_fixture()
                try:
                    image, = self.source_image_audit(invocation)["images"]
                    canonical = report.read_json(invocation / "verdict.json")
                    owner, = canonical["expected"]
                    configured = {**owner, "images": [{"name": image["image"], "architecture": "amd64",
                                                        "archive": image["archive"]["path"]}], "source_commands": []}
                    contract = {"owners": {owner["label"]: [image["image"]]}, "configured_owners": [configured]}
                    if damage == "missing-owner":
                        contract["configured_owners"] = []
                    elif damage == "configuration":
                        configured["configuration"] = "other-config"
                    elif damage == "archive":
                        configured["images"][0]["archive"] = "bazel-out/other/image.tar"
                    elif damage == "build-unconfigured":
                        canonical.update(command_kind="build", executed=[])
                        contract.pop("configured_owners")
                    elif damage == "empty-unconfigured":
                        contract = {"owners": {owner["label"]: []}}
                    else:
                        configured["source_commands"] = [{"owner": owner["label"], "architecture": "x86_64",
                                                          "image_class": "cpu", "images": []}]
                    report.write_json(invocation / "verdict.json", canonical)
                    report.write_json(invocation / "expectations.json", contract)
                    value = self.source_image_audit(invocation)
                    self.assertEqual(value["audit_status"], "INCOMPLETE", value)
                    self.assertIn(expected_error, "\n".join(value["errors"]))
                finally:
                    shutil.rmtree(invocation)

    def test_source_images_keep_no_image_benchmark_measurement_obligations(self) -> None:
        invocation = self.source_image_fixture()
        canonical = report.read_json(invocation / "verdict.json")
        contract = report.read_json(invocation / "expectations.json")
        owner, = canonical["expected"]
        contract["owners"][owner["label"]] = []
        contract["configured_owners"] = [{**owner, "images": [], "source_commands": []}]
        records, spawns = report.compact_records(invocation / "execution.json")
        (invocation / "execution.json").write_text("\n".join(json.dumps(row) for row in
            [*records.values(), *({"spawn": spawn} for spawn in spawns if spawn.get("mnemonic") != "SourceImageOCI")]))
        report.write_json(invocation / "expectations.json", contract)
        value = self.source_image_audit(invocation)
        self.assertEqual(value["audit_status"], "PASS", value)
        contract["benchmarks"][owner["label"]][0]["name"] = "BenchmarkRequiredButAbsent"
        report.write_json(invocation / "expectations.json", contract)
        value = self.source_image_audit(invocation)
        self.assertEqual(value["audit_status"], "INCOMPLETE", value)
        self.assertIn("Measured benchmark names differ", "\n".join(value["errors"]))
        canonical.update(command_kind="build", executed=[])
        report.write_json(invocation / "verdict.json", canonical)
        value = self.source_image_audit(invocation)
        self.assertEqual(value["audit_status"], "INCOMPLETE", value)
        self.assertIn("benchmark", "\n".join(value["errors"]).lower())

    def source_contract_fixture(self, *, test: bool = False) -> tuple[Path, Path, Path]:
        if test:
            invocation = self.fixture()
        else:
            invocation, _, _ = self.build_output_fixture()
        events = self.events(invocation)
        target = next(event["id"]["targetCompleted"] for event in events if "completed" in event)
        identity = {**target, "aspect": report.SOURCE_IMAGE_ASPECT}
        contract = invocation / "owner.source-images.json"
        report.write_json(contract, {"schema_version": 1, "owner": target["label"],
                                     "images": [], "source_commands": []})
        digest = report.file_record(contract)
        events.extend([
            {"id": {"targetConfigured": {"label": target["label"], "aspect": report.SOURCE_IMAGE_ASPECT}},
             "configured": {"targetKind": "filegroup rule"}, "children": [{"targetCompleted": identity}]},
            {"id": {"targetCompleted": identity}, "completed": {"success": True, "outputGroup": [
                {"name": "source_image_contracts", "fileSets": [{"id": "source-contract"}]}]}},
            {"id": {"namedSet": {"id": "source-contract"}}, "namedSetOfFiles": {"files": [
                {"name": contract.name, "uri": contract.as_uri(), "digest": digest["sha256"],
                 "length": str(digest["bytes"])}]}},
        ])
        self.write_events(invocation, events)
        return invocation, self.analysis(invocation), contract

    def test_source_contract_capture_retains_aspect_and_ordinary_outputs(self) -> None:
        invocation, analysis, _ = self.source_contract_fixture()
        value = report.preflight(analysis, 0, "build")
        self.assertEqual(value["output_options"],
                         ["--output_groups=+default,+source_image_contracts", "--remote_download_outputs=toplevel"])
        captured = report.capture(invocation / "events.jsonl", invocation / "captured.jsonl", invocation,
                                  invocation / "reports", expected_analysis=(analysis,))
        self.assertEqual(captured["errors"], [], captured)
        self.assertEqual(self.verdict(invocation, analysis=analysis)["status"], "PASS")
        expectations = report.source_image_expectations(
            invocation / "verdict.json", report.manifest_path(invocation / "captured.jsonl"))
        configured, = expectations["configured_owners"]
        self.assertEqual((configured["images"], configured["source_commands"]), ([], []))
        self.assertEqual(expectations["owners"], {configured["label"]: []})
        self.assertEqual({row["group"] for row in captured["build_outputs"]}, {"default", "source_image_contracts"})

    def test_source_contract_capture_accepts_only_complete_test_no_build_analysis(self) -> None:
        invocation, analysis, _ = self.source_contract_fixture(test=True)
        events = [json.loads(line) for line in analysis.read_text().splitlines()]
        finished = next(event["finished"] for event in events if "finished" in event)
        finished.update(overallSuccess=False, exitCode={"name": "BUILD_FAILURE", "code": 1})
        completed = next(event["id"]["targetCompleted"] for event in self.events(invocation)
                         if "completed" in event)
        no_build = {"id": {"targetCompleted": completed}, "aborted": {"reason": "NO_BUILD"}}
        for kind, complete, want_exit in (("test", True, 0), ("build", True, 1), ("test", False, 1)):
            with self.subTest(kind=kind, complete=complete):
                analysis.write_text("".join(json.dumps(event) + "\n" for event in
                                            events + ([no_build] if complete else [])))
                args = ["qualification_report.py", "capture", "--events", str(invocation / "events.jsonl"),
                        "--output", str(invocation / "captured.jsonl"), "--output-root", str(invocation),
                        "--report-dir", str(invocation / "reports"), "--expected-analysis", str(analysis),
                        "--expected-kind", kind]
                with mock.patch.object(sys, "argv", args):
                    self.assertEqual(report.main(), want_exit)
                captured = report.read_json(report.manifest_path(invocation / "captured.jsonl"))
                self.assertEqual({row["name"] for row in captured["reports"]}, {"test.log", "test.xml"})
                if want_exit:
                    self.assertEqual(captured["build_outputs"], [])
                    self.assertIn("Expected successful, nonempty target analysis", "\n".join(captured["errors"]))
                else:
                    self.assertEqual(captured["errors"], [])
                    self.assertEqual({row["group"] for row in captured["build_outputs"]}, {"source_image_contracts"})
                    self.assertEqual(self.verdict(invocation, analysis=analysis)["status"], "PASS")
                    contract = report.source_image_expectations(
                        invocation / "verdict.json", report.manifest_path(invocation / "captured.jsonl"))
                    owner, = contract["configured_owners"]
                    self.assertEqual((owner["images"], owner["source_commands"]), ([], []))

    def test_source_contract_alias_requires_its_configured_actual(self) -> None:
        invocation, analysis, path = self.source_contract_fixture()
        self.addCleanup(shutil.rmtree, invocation)
        events = self.events(invocation)
        target = next(event for event in events if event.get("configured", {}).get("targetKind") != "alias rule"
                      and "configured" in event and "aspect" not in event["id"]["targetConfigured"])
        target["configured"]["targetKind"] = "alias rule"
        contract = report.read_json(path)
        contract["owner"] = "//other/package:implementation"
        contract["images"] = [{"name": "mandatory", "architecture": "amd64", "archive": "image.tar"}]
        report.write_json(path, contract)
        digest = report.file_record(path)
        file_set = next(event for event in events if event["id"].get("namedSet", {}).get("id") == "source-contract")
        file_set["namedSetOfFiles"]["files"][0].update(digest=digest["sha256"], length=str(digest["bytes"]))
        self.write_events(invocation, events)
        analysis = self.analysis(invocation)
        selection = report.preflight(analysis, 0, "build")
        report.write_json(invocation / "selection.json", selection)
        alias, = selection["source_image_aliases"]
        report.capture(invocation / "events.jsonl", invocation / "captured.jsonl", invocation,
                       invocation / "reports", expected_analysis=(analysis,))
        self.assertEqual(self.verdict(invocation, analysis=analysis)["status"], "PASS")
        verdict = invocation / "verdict.json"
        manifest = report.manifest_path(invocation / "captured.jsonl")
        with self.assertRaises(FileNotFoundError):
            report.source_image_expectations(verdict, manifest)
        different_config = "a" * 64
        attribute = {"name": "actual", "type": "LABEL", "stringValue": contract["owner"]}
        edge = {"label": contract["owner"], "configurationChecksum": different_config}
        alias_rule = {"name": alias["label"], "ruleClass": "alias", "attribute": [attribute], "configuredRuleInput": [edge]}
        actual_rule = {"name": contract["owner"], "ruleClass": "filegroup"}
        requested = {"checksum": alias["configuration"]}
        actual = {"checksum": different_config}
        graph = {"results": [{"target": {"rule": alias_rule}, "configuration": requested},
                             {"target": {"rule": actual_rule}, "configuration": actual}]}
        graph_path = invocation / "source-image-aliases.json"
        report.write_json(graph_path, graph)
        owner, = report.source_image_expectations(verdict, manifest)["configured_owners"]
        self.assertEqual((owner["label"], owner["producer"]["label"], owner["producer"]["configuration"], owner["images"]),
                         (alias["label"], contract["owner"], different_config, contract["images"]))
        for row, key in ((alias_rule, "ruleClass"), (attribute, "stringValue"), (edge, "label"),
                         (edge, "configurationChecksum"), (requested, "checksum"), (actual, "checksum")):
            original = row[key]
            row[key] = "unrelated"
            report.write_json(graph_path, graph)
            with self.subTest(key=key), self.assertRaises(ValueError):
                report.source_image_expectations(verdict, manifest)
            row[key] = original

    def test_source_contract_missing_or_wrong_aspect_cannot_pass(self) -> None:
        for damage in ("missing", "wrong-aspect", "changed-bytes"):
            with self.subTest(damage=damage):
                invocation, analysis, contract = self.source_contract_fixture()
                try:
                    events = self.events(invocation)
                    completed = next(event for event in events if event.get("id", {}).get("targetCompleted", {}).get("aspect"))
                    if damage == "missing":
                        completed["completed"]["outputGroup"] = []
                    elif damage == "wrong-aspect":
                        completed["id"]["targetCompleted"].pop("aspect")
                    else:
                        contract.write_text("changed after advertisement")
                    self.write_events(invocation, events)
                    report.capture(invocation / "events.jsonl", invocation / "captured.jsonl", invocation,
                                   invocation / "reports", expected_analysis=(analysis,))
                    value = self.verdict(invocation, analysis=analysis)
                    self.assertEqual(value["status"], "INCOMPLETE", value)
                finally:
                    shutil.rmtree(invocation)

    def test_source_contracts_bind_configuration_and_preserve_dynamic_builders(self) -> None:
        invocation, analysis, _ = self.source_contract_fixture()
        report.capture(invocation / "events.jsonl", invocation / "captured.jsonl", invocation,
                       invocation / "reports", expected_analysis=(analysis,))
        original = self.verdict(invocation, analysis=analysis)
        first, = [row for row in original["build_outputs"] if row["group"] == "source_image_contracts"]
        owner, = original["expected"]
        second_path = invocation / "second-contract.json"
        image = {"name": "basic/alpine", "architecture": "arm64", "archive": "bazel-out/arm64/bin/image.tar"}
        command = {"owner": "//images:cpu", "architecture": "aarch64", "image_class": "cpu", "images": [],
                   "contexts": "bazel-out/arm64/bin/contexts.tar", "context_images": [
                       {"name": "context-only", "architecture": "arm64", "archive": "bazel-out/arm64/bin/context-image.tar"}]}
        report.write_json(second_path, {"schema_version": 1, "owner": owner["label"],
                                        "images": [image], "source_commands": [command]})
        second = {**first, "identity": {"label": owner["label"], "configuration": {"id": "other-config"}},
                  "path": second_path.name, **report.file_record(second_path)}
        value = {**original, "expected": [owner, {**owner, "configuration": "other-config"}],
                 "build_outputs": [first, second]}
        report.write_json(invocation / "verdict.json", value)
        manifest = report.manifest_path(invocation / "captured.jsonl")
        actual = report.source_image_expectations(invocation / "verdict.json", manifest)
        by_config = {row["configuration"]: row for row in actual["configured_owners"]}
        self.assertEqual(by_config[owner["configuration"]]["images"], [])
        self.assertEqual(by_config["other-config"]["images"], [image])
        self.assertEqual(by_config["other-config"]["source_commands"], [command])
        self.assertEqual(actual["owners"][owner["label"]], ["basic/alpine"])
        for outputs in ([first], [first, first], [first, {**second, "identity": first["identity"]}]):
            report.write_json(invocation / "verdict.json", {**value, "build_outputs": outputs})
            with self.subTest(outputs=outputs), self.assertRaises(ValueError):
                report.source_image_expectations(invocation / "verdict.json", manifest)

    def test_registry_pins_require_exact_configured_retained_output(self) -> None:
        invocation, analysis, _ = self.source_contract_fixture()
        report.capture(invocation / "events.jsonl", invocation / "captured.jsonl", invocation,
                       invocation / "reports", expected_analysis=(analysis,))
        original = self.verdict(invocation, analysis=analysis)
        output, = [row for row in original["build_outputs"] if row["group"] == "source_image_contracts"]
        owner, = original["expected"]
        contract_path = invocation / output["path"]
        pin_path = invocation / "retained-pin"
        pin_name = "bazel-out/k8-fastbuild/bin/owner.source-image-pin-0"
        index_name = "bazel-out/k8-fastbuild/bin/owner.source-image-index-0.json"
        index_path = invocation / "retained-index.json"
        report.write_json(index_path, {"schemaVersion": 2, "manifests": []})
        report.write_json(contract_path, {"schema_version": 1, "owner": owner["label"], "source_commands": [],
            "images": [{"name": "pinned", "architecture": "amd64", "archive": "bazel-out/k8/bin/pin.tar",
                        "origin": "registry", "layout": "bazel-out/k8/bin/layout", "usage": "docker-load",
                        "config": "", "pin": pin_name, "index": index_name}]})
        output.update(report.file_record(contract_path))
        pin_path.write_text("sha256:" + "a" * 64 + "\n")
        pin = {**output, "name": pin_name, "path": pin_path.name, **report.file_record(pin_path)}
        index = {**output, "name": index_name, "path": index_path.name, **report.file_record(index_path)}
        for damage, error in ((None, None), ("owner", "Missing configured registry pin output"),
                              ("missing", "Missing configured registry pin output"),
                              ("bytes", "differs from retained build output"), ("format", "Invalid registry pin bytes")):
            with self.subTest(damage=damage):
                current = dict(pin)
                pin_path.write_text("sha256:" + "a" * 64 + "\n")
                if damage == "owner":
                    current["identity"] = {"label": owner["label"], "configuration": {"id": "another-config"}}
                elif damage in ("bytes", "format"):
                    pin_path.write_text("not-a-digest")
                    if damage == "format":
                        current.update(report.file_record(pin_path))
                report.write_json(invocation / "verdict.json", {**original, "build_outputs": [output, index] + ([] if damage == "missing" else [current])})
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        report.source_image_expectations(invocation / "verdict.json", report.manifest_path(invocation / "captured.jsonl"))
                else:
                    value = report.source_image_expectations(invocation / "verdict.json", report.manifest_path(invocation / "captured.jsonl"))
                    configured, = value["configured_owners"]
                    self.assertEqual(configured["pins"][pin_name]["digest"], "sha256:" + "a" * 64)

    def matrix_fixture(self, name: str = "pass", row_count: int = 1, *, source_contract: bool = False) -> tuple[Path, Path, dict[str, object]]:
        invocation = self.fixture("pass" if name == "all-skipped" else name)
        if name == "all-skipped":
            (invocation / "test.xml").write_text(
                '<testsuite><testcase name="unavailable"><skipped>environment</skipped></testcase></testsuite>')
        self.capture(invocation)
        original = self.verdict(invocation)
        if source_contract:
            owner, = original["expected"]
            contract = invocation / "owner.source-images.json"
            report.write_json(contract, {"schema_version": 1, "owner": owner["label"], "images": [], "source_commands": []})
            original["build_outputs"] = [{"group": "source_image_contracts", "name": contract.name, "path": contract.name,
                "identity": {"label": owner["label"], "configuration": {"id": owner["configuration"]}}, **report.file_record(contract)}]
            report.write_json(invocation / "verdict.json", original)
        plan = {"schema_version": 1, "profile": "full", "source": original["source"], "run_id": "123",
                "run_attempt": 1, "side_inputs": {"clock_source": "kvm-clock"},
                "rows": [{"id": f"row-{index}", "lanes": ["test"], "required_kinds": ["test"], "required_audits": []}
                         for index in range(row_count)]}
        plan_path = self.root / "matrix-plan.json"
        report.write_json(plan_path, plan)
        identity = {key: plan[key] for key in ("source", "run_id", "run_attempt")}
        plan_hash = report.file_record(plan_path)["sha256"]
        results = {"schema_version": 1, **identity, "plan_sha256": plan_hash,
                   "matrix_result": "success", "transfer_result": "success", "rows": []}
        (self.evidence / "lane-results.jsonl").write_text(json.dumps({"lanes": ["test"], "native_exit": original["native_exit"]}) + "\n")
        for row in plan["rows"]:
            report.write_json(self.evidence / "matrix-row.json", {
                "schema_version": 1, **identity, "plan_sha256": plan_hash,
                "side_inputs": plan["side_inputs"], "row": row})
            artifact = self.root / (row["id"] + ".tar.gz")
            report.bundle(self.evidence, original["native_exit"], original["capture_exit"], artifact)
            results["rows"].append({"id": row["id"], "artifact": {"path": artifact.name, **report.file_record(artifact)}})
        results_path = self.root / "matrix-results.json"
        report.write_json(results_path, results)
        return plan_path, results_path, original

    def test_matrix_cli_composes_rows_and_retains_authenticated_children(self) -> None:
        plan, results, original = self.matrix_fixture(row_count=2, source_contract=True)
        output = self.root / "matrix.tar.gz"
        directory = self.root / "composed"
        command = subprocess.run([sys.executable, report.__file__, "compose", "--plan", str(plan),
                                  "--results", str(results), "--directory", str(directory), "--output", str(output)],
                                 capture_output=True, text=True, check=False)
        self.assertEqual(command.returncode, 0, command.stderr + command.stdout)
        value = report.receipt(output, report.file_record(output)["sha256"], original["source"])
        self.assertEqual((value["status"], value["expected_owner_count"], value["executed_attempt_count"]), ("PASS", 2, 2))
        self.assertEqual(value["case_counts"], {key: count * 2 for key, count in original["case_counts"].items()})
        self.assertEqual(len({row["path"] for row in value["invocations"]}), 2)
        for row in value["matrix"]["rows"]:
            descriptor = row["artifact"]
            retained = report.bundle_member(output, descriptor["path"])
            self.assertEqual((hashlib.sha256(retained).hexdigest(), len(retained)),
                             (descriptor["sha256"], descriptor["bytes"]))
            self.assertEqual(row["receipt"]["status"], "PASS")

    def test_matrix_workflow_discovers_flat_and_named_artifacts(self) -> None:
        workflow = Path(__file__).parents[2] / ".github/workflows/build.yml"
        script = textwrap.dedent(workflow.read_text().split("python3 - <<'PYTHON'\n", 1)[1].split("\n        PYTHON", 1)[0])
        for row_count, flat, status in ((1, True, "PASS"), (2, True, "INCOMPLETE"), (2, False, "PASS")):
            with self.subTest(rows=row_count, flat=flat):
                plan_path, results_path, original = self.matrix_fixture(row_count=row_count)
                try:
                    plan = report.read_json(plan_path)
                    records = report.read_json(results_path)["rows"]
                    for record in records[:1] if flat else records:
                        received = self.root / "rows"
                        if not flat:
                            received /= f"qualification-row-{record['id']}-attempt-{plan['run_attempt']}"
                        received.mkdir(parents=True, exist_ok=True)
                        artifact = self.root / record["artifact"]["path"]
                        shutil.copyfile(artifact, received / "qualification.tar.gz")
                        (received / "qualification").mkdir()
                        (received / "qualification/verdict.json").write_bytes(report.bundle_member(artifact, "verdict.json"))
                    command = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False,
                        env=os.environ | {"RUNNER_TEMP": str(self.root), "QUALIFICATION_PLAN_JSON": plan_path.read_text().rstrip("\n"),
                                          "QUALIFICATION_MATRIX_RESULT": "success", "QUALIFICATION_TRANSFER_RESULT": "success"})
                    self.assertEqual(command.returncode, 0, command.stderr)
                    value = report.compose(plan_path, results_path, self.root / "composed", self.root / "matrix.tar.gz")
                    self.assertEqual(value["status"], status, value)
                    self.assertEqual(len(value["matrix"]["rows"]), 1 if flat else row_count)
                    self.assertEqual(value["case_counts"]["passed"], original["case_counts"]["passed"] * (1 if flat else row_count))
                    if status == "INCOMPLETE":
                        self.assertIn("Missing result rows: ['row-1']", value["errors"])
                finally:
                    shutil.rmtree(self.root / "rows", ignore_errors=True)
                    shutil.rmtree(self.root / "composed", ignore_errors=True)
                    shutil.rmtree(self.evidence)
                    self.evidence.mkdir()

    def test_matrix_preserves_failed_incomplete_and_zero_case_rows(self) -> None:
        for name, status in (("fail", "FAIL"), ("incomplete", "FAIL"), ("zero", "INCOMPLETE"), ("all-skipped", "INCOMPLETE")):
            with self.subTest(name=name):
                plan, results, original = self.matrix_fixture(name, row_count=2)
                try:
                    manifest = report.read_json(results)
                    manifest["rows"].pop()  # Missing siblings must not erase a retained failure.
                    report.write_json(results, manifest)
                    value = report.compose(plan, results, self.root / "composed", self.root / "matrix.tar.gz")
                    self.assertEqual(value["status"], status, value)
                    for key in ("case_counts", "failures", "missing", "skips", "zero_case_owners", "all_skipped_owners"):
                        self.assertEqual(value[key], original[key], key)
                    self.assertIn("Missing result rows", "\n".join(value["errors"]))
                finally:
                    shutil.rmtree(self.evidence)
                    self.evidence.mkdir()
                    shutil.rmtree(self.root / "composed")

    def test_matrix_rejects_wrong_identity_coverage_or_transfer(self) -> None:
        for fault in ("duplicate", "extra", "missing", "plan", "source", "attempt", "digest", "size", "transfer", "row", "side-inputs", "query-only", "partial-lanes", "duplicate-lanes", "declaration-missing", "declaration-changed"):
            with self.subTest(fault=fault):
                plan, results, _ = self.matrix_fixture()
                try:
                    manifest = report.read_json(results)
                    if fault == "duplicate":
                        manifest["rows"] *= 2
                    elif fault == "extra":
                        manifest["rows"].append({"id": "unexpected"})
                    elif fault == "missing":
                        manifest["rows"] = []
                    elif fault == "plan":
                        manifest["plan_sha256"] = "0" * 64
                    elif fault == "source":
                        manifest["source"] = "0" * 40
                    elif fault == "attempt":
                        manifest["run_attempt"] += 1
                    elif fault == "digest":
                        manifest["rows"][0]["artifact"]["sha256"] = "0" * 64
                    elif fault == "size":
                        manifest["rows"][0]["artifact"]["bytes"] += 1
                    elif fault == "transfer":
                        manifest["transfer_result"] = "failure"
                    else:
                        metadata = report.read_json(self.evidence / "matrix-row.json")
                        if fault == "row":
                            metadata["row"]["id"] = "different-row"
                        elif fault == "side-inputs":
                            metadata["side_inputs"] = {}
                        elif fault in ("partial-lanes", "duplicate-lanes"):
                            record = json.dumps({"lanes": ["test"], "native_exit": 0}) + "\n"
                            (self.evidence / "lane-results.jsonl").write_text("" if fault == "partial-lanes" else record * 2)
                        elif fault.startswith("declaration-"):
                            invocation = self.evidence / "invocation-pass"
                            declaration = invocation / "owner.source-image-pin-0"
                            declaration.write_text("sha256:" + "a" * 64 + "\n")
                            canonical = report.read_json(invocation / "verdict.json")
                            owner, = canonical["expected"]
                            canonical["build_outputs"] = [{"group": "source_image_contracts", "name": declaration.name,
                                "path": declaration.name, "identity": {"label": owner["label"],
                                    "configuration": {"id": owner["configuration"]}}, **report.file_record(declaration)}]
                            report.write_json(invocation / "verdict.json", canonical)
                            if fault == "declaration-missing":
                                declaration.unlink()
                            else:
                                declaration.write_text("changed after audit")
                        else:
                            invocation = self.evidence / "invocation-pass"
                            context = report.read_json(invocation / "context.json")
                            context.update(command_kind="command", executed_arguments="arguments.json")
                            report.write_json(invocation / "context.json", context)
                            report.write_json(invocation / "arguments.json", ["query", "tests(//test/benchmarks:continuous_tests)"])
                            (invocation / "stdout.log").write_text("//benchmarks:owner\n")
                            (invocation / "stderr.log").write_text("")
                            report.write_json(invocation / "verdict.json", report.command_verdict(invocation / "context.json"))
                        report.write_json(self.evidence / "matrix-row.json", metadata)
                        artifact = self.root / manifest["rows"][0]["artifact"]["path"]
                        report.bundle(self.evidence, 0, 0, artifact)
                        manifest["rows"][0]["artifact"].update(report.file_record(artifact))
                    report.write_json(results, manifest)
                    value = report.compose(plan, results, self.root / "composed", self.root / "matrix.tar.gz")
                    self.assertEqual(value["status"], "INCOMPLETE", value)
                    if fault == "query-only":
                        self.assertIn("Missing required command kinds", "\n".join(value["errors"]))
                        self.assertEqual(value["case_counts"], {})
                finally:
                    shutil.rmtree(self.evidence)
                    self.evidence.mkdir()
                    shutil.rmtree(self.root / "composed")

    def test_matrix_audits_require_each_execution_phase_and_bound_inputs(self) -> None:
        invocation = self.source_image_fixture()
        value = self.source_image_audit(invocation)
        self.assertEqual(value["audit_status"], "PASS", value)
        value["input_roles"] = {name: {"path": str(path), **report.file_record(path)}
                                for name, path in (("execution", invocation / "execution.json"),
                                                   ("verdict", invocation / "verdict.json"),
                                                   ("manifest", invocation / "manifest.json"),
                                                   ("expectations", invocation / "expectations.json"))}
        evidence = invocation / "audit.json"
        report.write_json(evidence, value)
        canonical = invocation / "verdict.json"
        relative = str(canonical.relative_to(self.evidence))
        phase = {"path": relative, **report.file_record(canonical), "verdict": report.read_json(canonical)}
        identity = {"source": value["source"], "run_id": "123", "run_attempt": 1}
        row = {"id": "images", "required_audits": ["source-images"]}
        entry = {"path": relative, "inputs": {
            name: {"path": str(path.relative_to(self.evidence)), **report.file_record(path)}
            for name, path in (("expectations", invocation / "expectations.json"), ("execution", invocation / "execution.json"),
                              ("verdict", canonical), ("manifest", invocation / "manifest.json"))},
            "evidence": {"path": str(evidence.relative_to(self.evidence)), **report.file_record(evidence)}}
        audit = {"id": "source-images", **identity, "row_id": "images", "status": "PASS", "invocations": [entry]}
        producer = {"invocations": [phase], "matrix_audits": [audit]}
        artifact = self.root / "audit.tar.gz"
        report.archive_evidence(self.evidence, artifact)
        members = report.bundle_members(artifact, {entry["evidence"]["path"], entry["inputs"]["expectations"]["path"]})
        self.assertEqual(report.matrix_audits(members, producer, identity, row), [audit])
        for damage in ("missing", "duplicate", "partial", "input", "roles", "status"):
            changed = json.loads(json.dumps(producer))
            if damage == "missing":
                changed["matrix_audits"] = []
            elif damage == "duplicate":
                changed["matrix_audits"] *= 2
            elif damage == "partial":
                changed["invocations"].append({**phase, "path": "invocation-other/verdict.json"})
            elif damage == "input":
                changed["matrix_audits"][0]["invocations"][0]["inputs"]["execution"]["sha256"] = "0" * 64
            elif damage == "roles":
                inputs = changed["matrix_audits"][0]["invocations"][0]["inputs"]
                inputs["execution"], inputs["manifest"] = inputs["manifest"], inputs["execution"]
            else:
                changed["matrix_audits"][0]["status"] = "INCOMPLETE"
            with self.subTest(damage=damage), self.assertRaises(ValueError):
                report.matrix_audits(members, changed, identity, row)

        # A received artifact must also retain every canonical contract output,
        # including pin/index files that generated expectations refer to.
        contract_path = invocation / "owner.source-image-pin-0"
        contract_path.write_text("sha256:" + "a" * 64 + "\n")
        phase["verdict"]["build_outputs"] = [{"group": "source_image_contracts",
            "identity": {"label": phase["verdict"]["expected"][0]["label"],
                         "configuration": {"id": phase["verdict"]["expected"][0]["configuration"]}},
            "path": contract_path.name, "name": contract_path.name, **report.file_record(contract_path)}]
        with self.assertRaisesRegex(ValueError, "does not cover its configured outputs"):
            report.matrix_audits(members, producer, identity, row)

    def test_matrix_job_failure_preserves_incomplete_coverage(self) -> None:
        plan, results, _ = self.matrix_fixture()
        manifest = report.read_json(results)
        manifest["rows"] = []
        for conclusion in ("failure", "cancelled"):
            manifest["matrix_result"] = conclusion
            report.write_json(results, manifest)
            value = report.compose(plan, results, self.root / "composed", self.root / "matrix.tar.gz")
            self.assertEqual((value["status"], value["expected"], value["executed"]), ("FAIL", [], []))
            self.assertIn("Missing result rows", "\n".join(value["errors"]))

    def test_bundle_members_streams_requested_reports_once(self) -> None:
        invocation = self.fixture()
        artifact, _ = self.artifact(invocation, native=True)
        names = {"verdict.json", "invocation-pass/verdict.json", "invocation-pass/reports/test.xml"}
        with mock.patch.object(report.tarfile, "open", wraps=tarfile.open) as opened:
            members = report.bundle_members(artifact, names)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(members, {name: (self.evidence / name).read_bytes() for name in names})

    def test_bundle_members_rejects_missing_changed_and_duplicate_audit_inputs(self) -> None:
        invocation = self.fixture()
        artifact, _ = self.artifact(invocation, native=True)
        path = invocation / "test.log"
        name = str(path.relative_to(self.evidence))
        identity = report.file_record(path)
        original = path.read_bytes()
        for damage in ("missing", "changed", "duplicate-root", "duplicate-input"):
            with self.subTest(damage=damage):
                if damage == "missing":
                    path.unlink()
                elif damage == "changed":
                    path.write_bytes(b"different input")
                with tarfile.open(artifact, "w:gz") as archive:
                    for member in sorted(self.evidence.rglob("*")):
                        if member.is_file():
                            archive.add(member, arcname=str(member.relative_to(self.evidence)), recursive=False)
                    if damage.startswith("duplicate"):
                        duplicate = self.evidence / "verdict.json" if damage == "duplicate-root" else path
                        archive.add(duplicate, arcname=str(duplicate.relative_to(self.evidence)), recursive=False)
                with self.assertRaises(ValueError):
                    report.bundle_members(artifact, {"verdict.json"}, verify={name: identity})
                path.write_bytes(original)

    def test_matrix_uses_real_dispatch_plan_for_command_row(self) -> None:
        invocation = self.fixture()
        context = report.read_json(invocation / "context.json")
        context.update(command_kind="command", executed_arguments="arguments.json")
        report.write_json(invocation / "context.json", context)
        report.write_json(invocation / "arguments.json", ["run", "//:gazelle"])
        (invocation / "stdout.log").write_text("canonical command completed\n")
        (invocation / "stderr.log").write_text("")
        original = report.command_verdict(invocation / "context.json")
        report.write_json(invocation / "verdict.json", original)
        plan = report.unit_matrix.dispatch_plan(
            profile="single", source=original["source"], run_id="123", run_attempt=1,
            lanes=["gazelle"], architecture="amd64", execution="remote", requirements="",
            targets=[], test_filter="", syscall_bucket="", header_base="", cos_sha256="",
            cos_gzip_base64="", clock_source="calibrated", known_lanes=["gazelle"],
            pgo_manifest=LIFECYCLE_MANIFEST.with_name("pgo.json"))
        plan_path = self.root / "actual-plan.json"
        report.write_json(plan_path, plan)
        row, = plan["rows"]
        identity = {key: plan[key] for key in ("source", "run_id", "run_attempt")}
        metadata = {"schema_version": 1, **identity, "plan_sha256": report.file_record(plan_path)["sha256"],
                    "side_inputs": plan["side_inputs"], "row": row}
        report.write_json(self.evidence / "matrix-row.json", metadata)
        (self.evidence / "lane-results.jsonl").write_text(json.dumps({"lanes": row["lanes"], "native_exit": 0}) + "\n")
        artifact = self.root / "command.tar.gz"
        report.bundle(self.evidence, 0, 0, artifact)
        results = self.root / "actual-results.json"
        report.write_json(results, {"schema_version": 1, **identity, "plan_sha256": metadata["plan_sha256"],
                                   "matrix_result": "success", "transfer_result": "success", "rows": [
                                       {"id": row["id"], "artifact": {"path": artifact.name, **report.file_record(artifact)}}]})
        value = report.compose(plan_path, results, self.root / "composed", self.root / "composed.tar.gz")
        self.assertEqual((value["status"], value["case_counts"]), ("PASS", {}), value)
        command, = value["commands"]
        self.assertEqual((command["bazel_command"], command["native_exit"]), ("run", 0))

    def test_row_audit_preserves_unsupported_or_missing_phase(self) -> None:
        invocation, analysis, _ = self.source_contract_fixture()
        report.capture(invocation / "events.jsonl", invocation / "captured.jsonl", invocation,
                       invocation / "reports", expected_analysis=(analysis,))
        original = self.verdict(invocation, analysis=analysis)
        # The production wrapper uses events.jsonl for its captured projection.
        shutil.copyfile(report.manifest_path(invocation / "captured.jsonl"),
                        report.manifest_path(invocation / "events.jsonl"))
        report.write_json(self.evidence / "matrix-row.json", {
            "source": original["source"], "run_id": "123", "run_attempt": 1,
            "row": {"id": "images", "required_audits": ["source-images"]}})
        (invocation / "execution.binpb").write_bytes(b"retained compact log")
        for status in ("PASS", "INCOMPLETE"):
            with mock.patch.object(report, "audit_source_images", return_value={"audit_status": status}) as audit:
                value = report.source_image_row(self.evidence, self.root / "bb")
            self.assertEqual(value["audits"][0]["status"], status, value)
            entry, = value["audits"][0]["invocations"]
            self.assertEqual(entry["path"], "invocation-pass/verdict.json")
            self.assertEqual(audit.call_args.args[:1], (invocation / "execution.binpb",))
            self.assertEqual(report.read_json(invocation / "verdict.json"), original)
        (invocation / "execution.binpb").unlink()
        value = report.source_image_row(self.evidence, self.root / "bb")
        self.assertEqual(value["audits"][0]["status"], "INCOMPLETE", value)
        self.assertEqual(report.read_json(invocation / "verdict.json"), original)

    def artifact(self, invocation: Path, *, native: bool = False) -> tuple[Path, dict[str, object]]:
        self.capture(invocation)
        value = self.verdict(invocation)
        bundle = self.root / "qualification.tar.gz"
        producer = report.bundle(self.evidence, value["native_exit"], value["capture_exit"], bundle)
        if native:
            return bundle, producer
        artifact = self.root / "artifact.zip"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.write(bundle, "qualification.tar.gz")
            archive.write(self.evidence / "verdict.json", "qualification/verdict.json")
        return artifact, producer

    def test_receipt_preserves_retained_outcomes(self) -> None:
        outcomes = (("pass", "PASS"), ("fail", "FAIL"), ("incomplete", "FAIL"),
                    ("zero", "INCOMPLETE"), ("all-skipped", "INCOMPLETE"))
        for (name, status), native in itertools.product(outcomes, (False, True)):
            with self.subTest(name=name, native=native):
                invocation = self.fixture("pass" if name == "all-skipped" else name)
                if name == "all-skipped":
                    (invocation / "test.xml").write_text(
                        '<testsuite><testcase name="unavailable"><skipped>environment</skipped></testcase></testsuite>')
                artifact, producer = self.artifact(invocation, native=native)
                digest = report.file_record(artifact)["sha256"]
                output = self.root / "receipt.json"
                command = subprocess.run(
                    [sys.executable, report.__file__, "receipt", "--artifact", str(artifact),
                     "--sha256", "sha256:" + digest, "--expected-source", producer["sources"][0],
                     "--output", str(output)], capture_output=True, text=True, check=False)
                self.assertEqual(command.returncode, int(status != "PASS"), command.stderr)
                self.assertEqual(command.stdout.strip(), f"{status}: {output}")
                value = report.read_json(output)
                for key in ("status", "case_counts", "failures", "missing", "skips", "zero_case_owners",
                            "all_skipped_owners", "native_exits", "verdict_available_at"):
                    self.assertEqual(value[key], producer[key], key)
                self.assertEqual(value["invocations"][0]["errors"], producer["invocations"][0]["verdict"]["errors"])
                self.assertEqual(value["expected_owner_count"], len(producer["expected"]))
                self.assertEqual(value["executed_attempt_count"], len(producer["executed"]))
                phase = producer["invocations"][0]["verdict"]
                self.assertEqual(value["invocations"][0]["expected_owner_count"], len(phase["expected"]))
                self.assertEqual(value["invocations"][0]["executed_attempt_count"], len(phase["executed"]))
                self.assertGreaterEqual(report.timestamp(value["consumer_verified_at"]),
                                        report.timestamp(value["verdict_available_at"]))
                self.assertEqual(value["invocations"][0]["execution_finished_at"],
                                 producer["invocations"][0]["verdict"]["execution_finished_at"])
                shutil.rmtree(invocation)

    def test_receipt_rejects_wrong_artifact_source_and_targets(self) -> None:
        invocation = self.fixture()
        for native in (False, True):
            artifact, producer = self.artifact(invocation, native=native)
            digest = report.file_record(artifact)["sha256"]
            source = producer["sources"][0]
            targets = [row["label"] for row in producer["expected"]]
            self.assertEqual(report.receipt(artifact, digest, source, targets)["status"], "PASS")
            for sha256, commit, labels, message in (
                ("0" * 64, source, targets, "Artifact SHA256"),
                (digest, "0" * 40, targets, "Producer sources"),
                (digest, source, ["//wrong:target"], "Producer target labels"),
            ):
                with self.subTest(native=native, message=message), self.assertRaisesRegex(ValueError, message):
                    report.receipt(artifact, sha256, commit, labels)

    def test_receipt_rejects_oversized_verdict(self) -> None:
        invocation = self.fixture()
        for native in (False, True):
            artifact, producer = self.artifact(invocation, native=native)
            digest = report.file_record(artifact)["sha256"]
            with self.subTest(native=native), mock.patch.object(report, "MAX_REPORT_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "report size bound"):
                    report.receipt(artifact, digest, producer["sources"][0])

    def test_receipt_rejects_missing_or_linked_native_verdict(self) -> None:
        artifact = self.root / "bundle.tar.gz"
        for linked in (False, True):
            with tarfile.open(artifact, "w:gz") as archive:
                if linked:
                    member = tarfile.TarInfo("verdict.json")
                    member.type = tarfile.SYMTYPE
                    member.linkname = str(FIXTURES / "pass/context.json")
                    archive.addfile(member)
            digest = report.file_record(artifact)["sha256"]
            with self.subTest(linked=linked), self.assertRaisesRegex(ValueError, "regular file" if linked else "missing"):
                report.receipt(artifact, digest, "0" * 40)

    def test_receipt_command_rejects_corrupt_native_bundle(self) -> None:
        artifact, producer = self.artifact(self.fixture(), native=True)
        artifact.write_bytes(artifact.read_bytes()[:32])
        digest = report.file_record(artifact)["sha256"]
        output = self.root / "receipt.json"
        command = subprocess.run(
            [sys.executable, report.__file__, "receipt", "--artifact", str(artifact),
             "--sha256", digest, "--expected-source", producer["sources"][0],
             "--output", str(output)], capture_output=True, text=True, check=False)
        self.assertNotEqual(command.returncode, 0)
        self.assertNotIn("Traceback", command.stderr)
        self.assertFalse(output.exists())

    def test_actions_collection_retains_missing_artifact_failure(self) -> None:
        retained = report.read_json(FIXTURES / "actions-no-artifact.json")
        original = retained["workflow"]
        for conclusion, status in (("failure", "FAIL"), ("success", "INCOMPLETE")):
            with self.subTest(conclusion=conclusion):
                metadata = {**original, "conclusion": conclusion}
                pending = {**metadata, "status": "in_progress", "conclusion": None}
                responses = [json.dumps(value).encode() for value in (pending, metadata, retained["artifacts"])]
                with (mock.patch.object(collect_actions.subprocess, "check_output", side_effect=responses),
                      mock.patch.object(collect_actions.subprocess, "run", return_value=subprocess.CompletedProcess([], 1))):
                    value = collect_actions.collect_actions(original["id"], original["head_sha"], "amd64", self.root / "actions")
                self.assertEqual((value["status"], value["collection_status"]), (status, "INCOMPLETE"))
                self.assertIn("got 0", value["collection_error"])
                self.assertEqual((value["workflow"]["conclusion"], value["watch_exit"]), (conclusion, 1))
                self.assertTrue(value["source_verified"])
                self.assertEqual(value["submitted_source"], original["head_sha"])
                self.assertNotIn("case_counts", value)
                self.assertNotIn("consumer_verified_at", value)
                self.assertEqual(report.read_json(self.root / "actions-receipt.json"), value)
                self.assertGreaterEqual(report.timestamp(value["collection_finished_at"]),
                                        report.timestamp(value["workflow"]["terminal_observed_at"]))

    def test_actions_collection_preserves_source_and_collection_errors(self) -> None:
        metadata = report.read_json(FIXTURES / "actions-no-artifact.json")["workflow"]
        for fault in ("source", "run", "download", "digest", "corrupt"):
            with self.subTest(fault=fault):
                current = dict(metadata)
                if fault == "source":
                    current["head_sha"] = "0" * 40
                if fault == "run":
                    current["id"] += 1
                artifacts = {"artifacts": [{"id": 1, "name": "qualification-amd64", "expired": False,
                                            "digest": "sha256:" + "0" * 64}]}
                if fault == "digest":
                    artifacts["artifacts"][0].pop("digest")
                responses = [json.dumps(value).encode() for value in (current, artifacts)]
                def download(command: list[str], *, stdout: BinaryIO, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
                    if fault == "download":
                        raise OSError("retained transfer failure")
                    stdout.write(b"corrupt artifact")
                    return subprocess.CompletedProcess(command, 0)
                with (mock.patch.object(collect_actions.subprocess, "check_output", side_effect=responses),
                      mock.patch.object(collect_actions.subprocess, "run", side_effect=download)):
                    value = collect_actions.collect_actions(metadata["id"], metadata["head_sha"], "amd64", self.root / "actions")
                self.assertEqual((value["status"], value["collection_status"]), ("FAIL", "INCOMPLETE"))
                self.assertEqual(value["workflow"]["head_sha"], current["head_sha"])
                self.assertEqual(value["source_verified"], fault not in ("source", "run"))
                self.assertEqual(value["submitted_source"], metadata["head_sha"])
                self.assertEqual(value["requested_run"], metadata["id"])
                self.assertNotIn("case_counts", value)
                self.assertNotIn("consumer_verified_at", value)
                self.assertTrue(value["collection_error"])
                if fault == "download":
                    self.assertIn("retained transfer failure", value["collection_error"])

    def test_actions_collection_preserves_authenticated_producer_outcomes(self) -> None:
        retained = report.read_json(FIXTURES / "actions-no-artifact.json")["workflow"]
        for name, conclusion, status in (("pass", "success", "PASS"), ("fail", "success", "FAIL"),
                                         ("zero", "success", "INCOMPLETE"), ("pass", "failure", "FAIL")):
            with self.subTest(fixture=name, conclusion=conclusion):
                invocation = self.fixture(name)
                artifact, producer = self.artifact(invocation)
                metadata = {**retained, "head_sha": producer["sources"][0], "conclusion": conclusion}
                artifacts = {"artifacts": [{"id": 1, "name": "qualification-amd64", "expired": False,
                                            "digest": "sha256:" + report.file_record(artifact)["sha256"]}]}
                pending = {**metadata, "status": "in_progress", "conclusion": None}
                responses = [json.dumps(value).encode() for value in (pending, pending, metadata, artifacts)]
                def download(command: list[str], *, stdout: BinaryIO, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
                    if command[:3] == ["gh", "run", "watch"]:
                        return subprocess.CompletedProcess(command, 1)
                    stdout.write(artifact.read_bytes())
                    return subprocess.CompletedProcess(command, 0)
                with (mock.patch.object(collect_actions.subprocess, "check_output", side_effect=responses),
                      mock.patch.object(collect_actions.subprocess, "run", side_effect=download)):
                    value = collect_actions.collect_actions(metadata["id"], metadata["head_sha"], "amd64", self.root / "actions")
                self.assertEqual((value["status"], value["producer_status"], value["collection_status"]),
                                 (status, producer["status"], "COMPLETE"))
                for field in ("case_counts", "failures", "missing", "zero_case_owners", "native_exits"):
                    self.assertEqual(value[field], producer[field])
                self.assertTrue(value["source_verified"])
                self.assertEqual(value["watch_exits"], [1, 1])
                shutil.rmtree(invocation)

    def test_actions_collection_retains_live_run_after_watch_failures(self) -> None:
        retained = report.read_json(FIXTURES / "actions-no-artifact.json")["workflow"]
        pending = {**retained, "status": "in_progress", "conclusion": None}
        with (mock.patch.object(collect_actions.subprocess, "check_output", return_value=json.dumps(pending).encode()),
              mock.patch.object(collect_actions.subprocess, "run", return_value=subprocess.CompletedProcess([], 1))):
            value = collect_actions.collect_actions(retained["id"], retained["head_sha"], "amd64", self.root / "actions")
        self.assertEqual(value["status"], "INCOMPLETE")
        self.assertEqual(value["watch_exits"], [1, 1, 1])
        self.assertEqual(value["workflow"]["id"], retained["id"])
        self.assertIsNone(value["actions_artifact"])
        self.assertIn("Workflow is still live", value["collection_error"])

    def test_actions_collection_shares_watch_budget_across_reattachments(self) -> None:
        retained = report.read_json(FIXTURES / "actions-no-artifact.json")["workflow"]
        pending = {**retained, "status": "in_progress", "conclusion": None}
        for options, clock, timeouts in (
            ([], [0, 0, 6000, 10000], [10000, 4000]),
            (["--watch-seconds", "18000"], [0, 11000, 17999, 18000], [7000, 1]),
        ):
            with self.subTest(options=options):
                argv = ["collect_actions", "--run", str(retained["id"]), "--source", retained["head_sha"],
                        "--architecture", "amd64", "--prefix", str(self.root / "budget"), *options]
                with (mock.patch.object(sys, "argv", argv),
                      mock.patch.object(collect_actions.time, "monotonic", side_effect=clock),
                      mock.patch.object(collect_actions.subprocess, "check_output", return_value=json.dumps(pending).encode()),
                      mock.patch.object(collect_actions.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as watch):
                    self.assertEqual(collect_actions.main(), 1)
                self.assertEqual([call.kwargs["timeout"] for call in watch.call_args_list], timeouts)
                value = report.read_json(self.root / "budget-receipt.json")
                self.assertEqual((value["status"], value["collection_status"]), ("INCOMPLETE", "INCOMPLETE"))
                self.assertEqual(value["watch_exits"], [1, 1])
                self.assertEqual(value["workflow"]["id"], retained["id"])
                self.assertIsNone(value["actions_artifact"])
                self.assertIn("Workflow is still live", value["collection_error"])

    def test_actions_collection_rejects_invalid_watch_budget(self) -> None:
        retained = report.read_json(FIXTURES / "actions-no-artifact.json")["workflow"]
        for budget in (0, -1, True):
            with self.subTest(budget=budget), mock.patch.object(collect_actions.subprocess, "check_output") as api:
                value = collect_actions.collect_actions(retained["id"], retained["head_sha"], "amd64",
                                                        self.root / "invalid-budget", watch_seconds=budget)
                self.assertEqual((value["status"], value["collection_status"]), ("INCOMPLETE", "INCOMPLETE"))
                self.assertIn("positive watch budget", value["collection_error"])
                api.assert_not_called()

    def test_actions_collection_rejects_prior_attempt_artifacts(self) -> None:
        retained = report.read_json(FIXTURES / "actions-no-artifact.json")["workflow"]
        artifact, producer = self.artifact(self.fixture())
        digest = "sha256:" + report.file_record(artifact)["sha256"]
        for name in ("qualification-amd64", "qualification-amd64-attempt-1"):
            for conclusion, status in (("success", "INCOMPLETE"), ("failure", "FAIL")):
                with self.subTest(name=name, conclusion=conclusion):
                    metadata = {**retained, "head_sha": producer["sources"][0],
                                "run_attempt": 2, "conclusion": conclusion}
                    artifacts = {"artifacts": [{"id": 1, "name": name, "expired": False, "digest": digest}]}
                    responses = [json.dumps(value).encode() for value in (metadata, artifacts)]
                    with (mock.patch.object(collect_actions.subprocess, "check_output", side_effect=responses),
                          mock.patch.object(collect_actions.subprocess, "run") as download):
                        value = collect_actions.collect_actions(metadata["id"], metadata["head_sha"], "amd64",
                                                                self.root / (name + conclusion))
                    self.assertEqual((value["status"], value["collection_status"]), (status, "INCOMPLETE"))
                    self.assertEqual(value["bound_run_attempt"], 2)
                    self.assertIn("qualification-amd64-attempt-2 artifact, got 0", value["collection_error"])
                    self.assertIsNone(value["actions_artifact"])
                    self.assertNotIn("case_counts", value)
                    download.assert_not_called()

    def test_actions_collection_selects_current_attempt(self) -> None:
        retained = report.read_json(FIXTURES / "actions-no-artifact.json")["workflow"]
        artifact, producer = self.artifact(self.fixture("fail"))
        digest = "sha256:" + report.file_record(artifact)["sha256"]
        metadata = {**retained, "head_sha": producer["sources"][0], "run_attempt": 2, "conclusion": "success"}
        current = {"id": 3, "name": "qualification-amd64-attempt-2", "expired": False, "digest": digest}
        artifacts = {"artifacts": [
            {**current, "id": 1, "name": "qualification-amd64"},
            {**current, "id": 2, "name": "qualification-amd64-attempt-1"},
            current,
            {**current, "id": 4, "name": "qualification-arm64-attempt-2"},
        ]}
        responses = [json.dumps(value).encode() for value in (metadata, artifacts)]
        def download(command: list[str], *, stdout: BinaryIO, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            self.assertEqual(command[-1], "repos/tamird/gvisor/actions/artifacts/3/zip")
            stdout.write(artifact.read_bytes())
            return subprocess.CompletedProcess(command, 0)
        with (mock.patch.object(collect_actions.subprocess, "check_output", side_effect=responses),
              mock.patch.object(collect_actions.subprocess, "run", side_effect=download)):
            value = collect_actions.collect_actions(metadata["id"], metadata["head_sha"], "amd64",
                                                    self.root / "current", run_attempt=2)
        self.assertEqual((value["status"], value["producer_status"], value["collection_status"]),
                         ("FAIL", "FAIL", "COMPLETE"))
        self.assertEqual((value["requested_run_attempt"], value["bound_run_attempt"]), (2, 2))
        self.assertEqual(value["actions_artifact"], {"id": 3, "name": current["name"], "digest": digest})
        self.assertEqual(value["case_counts"], producer["case_counts"])

    def test_actions_collection_rejects_changed_or_invalid_attempt(self) -> None:
        retained = report.read_json(FIXTURES / "actions-no-artifact.json")["workflow"]
        for first, last, requested, error in (
            (1, 2, None, "expected 1, got 2"),
            (2, 2, 1, "expected 1, got 2"),
            (None, None, None, "positive integer"),
            (True, True, None, "positive integer"),
            (0, 0, None, "positive integer"),
            (1, 1, 0, "positive workflow run attempt"),
        ):
            with self.subTest(first=first, last=last, requested=requested):
                pending = {**retained, "status": "in_progress", "conclusion": None, "run_attempt": first}
                completed = {**retained, "conclusion": "success", "run_attempt": last}
                responses = [json.dumps(value).encode() for value in (pending, completed)]
                with (mock.patch.object(collect_actions.subprocess, "check_output", side_effect=responses),
                      mock.patch.object(collect_actions.subprocess, "run", return_value=subprocess.CompletedProcess([], 0))):
                    value = collect_actions.collect_actions(retained["id"], retained["head_sha"], "amd64",
                                                            self.root / "attempt", run_attempt=requested)
                self.assertEqual((value["status"], value["collection_status"]), ("INCOMPLETE", "INCOMPLETE"))
                self.assertIn(error, value["collection_error"])
                self.assertIsNone(value["actions_artifact"])
                self.assertNotIn("case_counts", value)

    def native_metadata(self, artifact: Path, producer: dict[str, object]) -> dict[str, object]:
        identity = report.file_record(artifact)
        return {"id": {"invocationId": "12345678-1234-1234-1234-123456789abc"},
                "invocationStatus": "COMPLETE_INVOCATION_STATUS", "success": producer["status"] == "PASS",
                "bazelExitCode": "OK" if producer["status"] == "PASS" else "BUILD_FAILURE",
                "commitSha": producer["sources"][0], "updatedAtUsec": "1792329302885000",
                "artifacts": [{"name": "qualification.tar.gz",
                               "uri": f"bytestream://remote.buildbuddy.io/blobs/{identity['sha256']}/{identity['bytes']}"}]}

    def native_collect(self, metadata: dict[str, object], responses: list[object]) -> dict[str, object]:
        key = self.root / "api-key"
        key.write_text("test-secret")
        with mock.patch.object(report, "HTTPSConnection") as connection:
            connection.return_value.getresponse.side_effect = responses
            value = report.native_receipt(metadata["id"]["invocationId"], key, self.root / "download.tar.gz",
                                          self.root / "native-receipt.json", metadata["commitSha"], None, 30)
        self.assertNotIn("test-secret", json.dumps(value))
        return value

    def native_response(self, data: bytes, status: int = 200) -> mock.MagicMock:
        response = mock.MagicMock(wraps=io.BytesIO(data))
        response.__enter__.return_value = response
        response.__exit__.side_effect = lambda *_: response.close()
        response.status = status
        return response

    def metadata_response(self, metadata: dict[str, object]) -> mock.MagicMock:
        return self.native_response(json.dumps({"invocation": [metadata]}).encode())

    def test_native_collection_preserves_retained_outcomes(self) -> None:
        for name, status in (("pass", "PASS"), ("fail", "FAIL"), ("incomplete", "FAIL"),
                             ("zero", "INCOMPLETE"), ("all-skipped", "INCOMPLETE")):
            with self.subTest(name=name):
                invocation = self.fixture("pass" if name == "all-skipped" else name)
                if name == "all-skipped":
                    (invocation / "test.xml").write_text(
                        '<testsuite><testcase name="unavailable"><skipped>environment</skipped></testcase></testsuite>')
                artifact, producer = self.artifact(invocation, native=True)
                metadata = self.native_metadata(artifact, producer)
                # A successful coordinator can still publish incomplete test
                # coverage. Preserve that producer result independently.
                metadata["success"] = status != "FAIL"
                pending = {**metadata, "invocationStatus": "PARTIAL_INVOCATION_STATUS", "artifacts": []}
                with mock.patch.object(report.time, "sleep"):
                    value = self.native_collect(metadata, [self.metadata_response(pending),
                                                           TimeoutError("timed out"),
                                                           OSError(errno.EADDRNOTAVAIL, "no local address"),
                                                           ConnectionResetError("connection reset"),
                                                           RemoteDisconnected("connection closed"),
                                                           self.metadata_response(metadata),
                                                           self.native_response(artifact.read_bytes())])
                self.assertEqual([retry["error"] for retry in value["observation_retries"]],
                                 ["TimeoutError", "EADDRNOTAVAIL", "ConnectionResetError", "RemoteDisconnected"])
                self.assertEqual((value["status"], value["producer_status"], value["collection_status"]),
                                 (status, status, "COMPLETE"))
                for field in ("case_counts", "failures", "missing", "skips", "zero_case_owners",
                              "all_skipped_owners", "native_exits"):
                    self.assertEqual(value[field], producer[field], field)
                self.assertEqual(value["native_invocation"]["updatedAtUsec"], metadata["updatedAtUsec"])
                self.assertEqual(report.read_json(self.root / "native-receipt.json"), value)
                # Reattachment reuses the authenticated blob; a second download
                # would exhaust these responses and fail the test.
                resumed = self.native_collect(metadata, [self.metadata_response(metadata)])
                self.assertEqual((resumed["status"], resumed["collection_status"]), (status, "COMPLETE"))
                (self.root / "download.tar.gz").unlink()
                shutil.rmtree(invocation)

    def test_native_collection_retains_failure_when_bundle_is_missing_or_invalid(self) -> None:
        artifact, producer = self.artifact(self.fixture("fail"), native=True)
        metadata = self.native_metadata(artifact, producer)
        for fault in ("missing", "duplicate", "corrupt", "truncated", "redirect", "source",
                      "omitted-success", "string-success", "integer-success"):
            with self.subTest(fault=fault):
                current = dict(metadata)
                data = artifact.read_bytes()
                status = 200
                if fault == "missing":
                    current["artifacts"] = []
                elif fault == "duplicate":
                    current["artifacts"] = metadata["artifacts"] * 2
                elif fault == "corrupt":
                    data = b"x" * len(data)
                elif fault == "truncated":
                    data = data[:32]
                elif fault == "redirect":
                    status = 302
                elif fault == "source":
                    current["commitSha"] = "0" * 40
                elif fault == "omitted-success":
                    current.pop("success")
                    current["artifacts"] = []
                else:
                    current["success"] = "false" if fault == "string-success" else 0
                responses = [self.metadata_response(current)]
                if fault in ("corrupt", "truncated", "redirect"):
                    responses.append(self.native_response(data, status))
                value = self.native_collect(metadata, responses)
                expected = "INCOMPLETE" if fault in ("string-success", "integer-success") else "FAIL"
                self.assertEqual((value["status"], value["collection_status"]), (expected, "ERROR"))
                self.assertEqual(value["native_invocation"]["bazelExitCode"], "BUILD_FAILURE")
                self.assertNotIn("consumer_verified_at", value)
                self.assertFalse((self.root / "download.tar.gz").exists())

    def test_native_collection_distinguishes_coordinator_failure_and_disconnection(self) -> None:
        artifact, producer = self.artifact(self.fixture(), native=True)
        metadata = self.native_metadata(artifact, producer)
        for state, expected in (("COMPLETE_INVOCATION_STATUS", "FAIL"),
                                ("DISCONNECTED_INVOCATION_STATUS", "INCOMPLETE")):
            with self.subTest(state=state):
                metadata.update(invocationStatus=state, success=False, bazelExitCode="INTERRUPTED")
                responses = [self.metadata_response(metadata)]
                if not (self.root / "download.tar.gz").exists():
                    responses.append(self.native_response(artifact.read_bytes()))
                value = self.native_collect(metadata, responses)
                self.assertEqual((value["status"], value["producer_status"]), (expected, "PASS"))
                self.assertEqual(value["native_invocation"]["bazelExitCode"], "INTERRUPTED")

    def test_native_collection_timeout_is_not_an_execution_verdict(self) -> None:
        artifact, producer = self.artifact(self.fixture(), native=True)
        metadata = self.native_metadata(artifact, producer)
        pending = {**metadata, "invocationStatus": "PARTIAL_INVOCATION_STATUS", "artifacts": []}
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                clock = [0]

                def expire(_: float) -> None:
                    clock[0] = 31

                responses = [TimeoutError("timed out")] if timeout else [self.metadata_response(pending)]
                with mock.patch.object(report.time, "monotonic", side_effect=lambda: clock[0]), \
                     mock.patch.object(report.time, "sleep", side_effect=expire):
                    value = self.native_collect(metadata, responses)
                self.assertEqual((value["status"], value["collection_status"]), ("INCOMPLETE", "ERROR"))
                self.assertIn("deadline", value["collection_error"])
                if not timeout:
                    self.assertEqual(value["native_invocation"]["invocationStatus"], "PARTIAL_INVOCATION_STATUS")
                self.assertNotIn("consumer_verified_at", value)

    def test_native_collection_deadline_covers_verification(self) -> None:
        original_open = Path.open
        original_receipt = report.receipt
        for name, phase in itertools.product(("pass", "fail"), ("hash", "archive", "verified")):
            with self.subTest(name=name, phase=phase):
                invocation = self.fixture(name)
                artifact, producer = self.artifact(invocation, native=True)
                metadata = self.native_metadata(artifact, producer)
                downloaded = self.root / "download.tar.gz"
                shutil.copyfile(artifact, downloaded)
                data = artifact.read_bytes()
                clock = [0]
                opens = [0]

                class SlowReader(io.BytesIO):
                    def read(self, size: int = -1) -> bytes:
                        chunk = super().read(size)
                        clock[0] = 31
                        return chunk

                def open_file(path: Path, mode: str = "r", *args: object, **kwargs: object) -> BinaryIO | TextIO:
                    if path == downloaded and mode == "rb":
                        opens[0] += 1
                        if (phase, opens[0]) in (("hash", 1), ("archive", 2)):
                            return SlowReader(data)
                    return original_open(path, mode, *args, **kwargs)

                def verify(path: Path, sha256: str, source: str, targets: list[str] | None = None,
                           *, deadline: float | None = None) -> dict[str, object]:
                    value = original_receipt(path, sha256, source, targets, deadline=deadline)
                    if phase == "verified":
                        clock[0] = 31
                    return value

                with mock.patch.object(report.time, "monotonic", side_effect=lambda: clock[0]), \
                     mock.patch.object(Path, "open", open_file), mock.patch.object(report, "receipt", verify):
                    value = self.native_collect(metadata, [self.metadata_response(metadata)])
                self.assertEqual((value["status"], value["collection_status"]),
                                 ("FAIL" if name == "fail" else "INCOMPLETE", "ERROR"))
                self.assertIn("deadline", value["collection_error"])
                self.assertEqual(value["native_invocation"]["success"], name == "pass")
                self.assertNotIn("consumer_verified_at", value)
                downloaded.unlink()
                shutil.rmtree(invocation)

    def test_native_collection_deadline_covers_metadata_reads(self) -> None:
        artifact, producer = self.artifact(self.fixture(), native=True)
        metadata = self.native_metadata(artifact, producer)
        response = self.metadata_response(metadata)
        clock = [0]

        def read1(size: int) -> bytes:
            clock[0] += 10
            return b" "

        response.read1.side_effect = read1
        with mock.patch.object(report.time, "monotonic", side_effect=lambda: clock[0]):
            value = self.native_collect(metadata, [response])
        self.assertEqual((value["status"], value["collection_status"]), ("INCOMPLETE", "ERROR"))
        self.assertIn("deadline", value["collection_error"])
        self.assertNotIn("consumer_verified_at", value)

    def test_bundle_command_logs_recoverable_archive_identity(self) -> None:
        invocation = self.fixture()
        self.capture(invocation)
        self.verdict(invocation)
        output = self.root / "bundle.tar.gz"
        for runner_exit, status in ((0, "PASS"), (17, "FAIL")):
            with self.subTest(runner_exit=runner_exit):
                arguments = [sys.executable, report.__file__, "bundle", "--directory", str(self.evidence),
                             "--runner-exit", str(runner_exit), "--capture-exit", "0", "--output", str(output)]
                command = subprocess.run(arguments, capture_output=True, text=True, check=False)
                self.assertEqual(command.returncode, runner_exit, command.stderr)
                receipt = json.loads(command.stdout)
                content = output.read_bytes()
                self.assertEqual(receipt["qualification_archive"],
                                 {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
                with tarfile.open(output) as archive:
                    verdict = json.load(archive.extractfile("verdict.json"))
                self.assertEqual(receipt["sources"], verdict["sources"])
                self.assertEqual(receipt["status"], status)
                read_fd, write_fd = os.pipe()
                os.close(read_fd)
                with os.fdopen(write_fd, "wb") as closed_pipe:
                    command = subprocess.run(arguments, stdout=closed_pipe, stderr=subprocess.PIPE, check=False)
                self.assertEqual(command.returncode, runner_exit or 1, command.stderr)

    def test_bundle_distinguishes_reporting_and_coordinator_failure(self) -> None:
        invocation = self.fixture()
        (invocation / "capture-exit").write_text("1\n")
        self.capture(invocation)
        self.verdict(invocation)
        output = self.root / "bundle.tar.gz"
        value = report.bundle(self.evidence, 1, 0, output)
        self.assertEqual((value["status"], value["native_exit"], value["runner_exit"]), ("INCOMPLETE", 0, 1))
        self.assertEqual(value["coordinator_failed"], False)
        (invocation / "capture-exit").write_text("0\n")
        self.verdict(invocation)
        value = report.bundle(self.evidence, 17, 0, output)
        self.assertEqual((value["status"], value["native_exit"], value["runner_exit"]), ("FAIL", 0, 17))
        self.assertEqual(value["coordinator_failed"], True)


if __name__ == "__main__":
    unittest.main()
