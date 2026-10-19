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
import hashlib
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
                declared_benchmarks: Path | None = None) -> dict[str, object]:
        events = invocation / "captured.jsonl"
        value = report.verdict(events, report.manifest_path(events), invocation / "context.json",
                               invocation / "expected.txt", (analysis,) if analysis else (), benchmarks, declared_benchmarks)
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

    def jtreg_fixture(self, output: str, *, zipped: bool, names: str = "First.java,Second.java") -> Path:
        invocation = self.fixture()
        events = self.events(invocation)
        configured = next(event["configured"] for event in events if "configured" in event)
        configured["tag"].append("jtreg-results")
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
                invocation = self.jtreg_fixture(output, zipped=zipped)
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

    def test_jtreg_section_stdout_is_not_result_framing(self) -> None:
        # Actual section output from DefaultProvider.java and the RMI tests
        # contains TEST: lines. A result-like stdout line is also ordinary text.
        stdout = ("TEST: Default provider with constructor\n"
                  "TEST: SHA1PRNG supported on all platforms by SUN provider\n"
                  "--------------------------------------------------\n"
                  "TEST: rmi\nTEST RESULT: Failed. printed by the test\n")
        for zipped in (False, True):
            with self.subTest(zipped=zipped):
                invocation = self.jtreg_fixture(
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
        invocation = self.jtreg_fixture(output, zipped=False)
        self.capture(invocation)
        value = self.verdict(invocation, analysis=self.analysis(invocation))
        self.assertEqual(value["status"], "PASS", value)
        self.assertEqual(value["executed"][0]["jtreg"]["results"],
                         {"First.java": "Passed.", "Second.java": "Passed."})

    def test_jtreg_skipped_result_is_not_a_passed_body(self) -> None:
        result = "Passed. Skipped: jtreg.SkippedException: Could not find NSS"
        invocation = self.jtreg_fixture(self.jtreg_output("First.java", result)
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
                invocation = self.jtreg_fixture(self.jtreg_output("First.java", "Passed."), zipped=False)
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
                invocation = self.jtreg_fixture(self.jtreg_output("First.java", "Failed."), zipped=False)
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

    def test_retained_test_no_build_preflight(self) -> None:
        events = FIXTURES / "analysis-no-build/events.jsonl"
        value = report.preflight(events, 1, "test")
        self.assertEqual((value["status"], value["native_exit"]), ("PASS", 1))
        self.assertEqual(len(value["expected"]), 2)
        self.assertEqual(value["test_output_options"], [])
        self.assertEqual(report.preflight(events, 1, "build")["status"], "INCOMPLETE")
        self.assertEqual(report.preflight(events, 4, "test")["status"], "INCOMPLETE")

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
        (invocation / "expected.txt").write_text("")
        (invocation / "native-exit").write_text("1\n")
        (invocation / "executed-arguments.txt").write_text("test\n--nobuild\n")
        context = report.read_json(invocation / "context.json")
        context["command_kind"] = "analysis"
        report.write_json(invocation / "context.json", context)
        self.capture(invocation)
        value = self.verdict(invocation)
        self.assertEqual((value["status"], value["native_exit"], value["accepted_analysis_exit"]), ("PASS", 1, True))
        combined = report.bundle(self.evidence, 0, 0, self.root / "bundle.tar.gz")
        self.assertEqual((combined["status"], combined["native_exit"]), ("PASS", 0))
        self.assertEqual(combined["native_exits"][0]["exit"], 1)

    def test_retained_failure_preserves_missing_shards(self) -> None:
        invocation = self.fixture("fail")
        self.assertEqual(self.capture(invocation)["errors"], [])
        value = self.verdict(invocation)
        self.assertEqual(value["status"], "FAIL")
        self.assertEqual(value["native_exit"], 3)
        self.assertEqual(value["case_counts"]["failed"], 1)
        self.assertEqual(sum(item.get("reason") == "announced result missing" for item in value["missing"]), 7)
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
                value = self.verdict(invocation, analysis=self.analysis(invocation))
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

    def test_bundle_retains_undeclared_outputs_only_for_failed_attempts(self) -> None:
        for status, name in itertools.product(("PASSED", "TIMEOUT"),
                                              ("test.outputs__outputs.zip", "test.outputs/guest/console.log")):
            with self.subTest(status=status, name=name):
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
                    if status == "PASSED":
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
        for phase in ("preflight", "execution", "command-pass", "command-fail", "command-timeout"):
            with self.subTest(phase=phase):
                workspace = self.root / phase
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
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
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
                    "QUALIFICATION_ARCH": "amd64",
                    "QUALIFICATION_LANES": "gazelle" if phase.startswith("command-") else "test",
                    "QUALIFICATION_TARGETS": "" if phase.startswith("command-") else "//test/syscalls:exec_binary_test_native_amd64",
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
                options = [] if phase.startswith("command-") else [f"--bazel-option={option}" for option in bazel_options]
                result = subprocess.run(
                    ["bash", "test/rbe/actions.sh", *options],
                    cwd=workspace, env=environment, capture_output=True, text=True, timeout=45, check=False,
                )
                self.assertEqual(result.returncode == 0, phase == "command-pass", result.stdout + result.stderr)
                self.assertEqual(started.exists(), phase != "preflight", result.stdout + result.stderr)
                if phase == "execution":
                    commands = [json.loads(line) for line in (workspace / "bazel-arguments.jsonl").read_text().splitlines()]
                    self.assertEqual(len(commands), 2, commands)
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
        original_copy = shutil.copyfileobj

        def fail_log(source, destination, length):
            if Path(source.name).name == "test.log":
                raise OSError("retained fixture copy failure")
            return original_copy(source, destination, length)

        with mock.patch.object(shutil, "copyfileobj", side_effect=fail_log):
            first = self.capture(invocation)
        self.assertEqual(len(first["errors"]), 1)
        self.assertEqual([row["name"] for row in first["reports"]], ["test.xml"])
        # The source XML can be gone: the previous verified copy is sufficient.
        (invocation / "test.xml").unlink()
        with mock.patch.object(shutil, "copyfileobj", wraps=original_copy) as copying:
            repaired = self.capture(invocation)
        self.assertEqual(copying.call_count, 1)
        self.assertEqual(repaired["errors"], [])
        self.assertEqual(self.verdict(invocation)["status"], "PASS")

    def test_resume_repairs_only_missing_or_corrupt_copies(self) -> None:
        invocation = self.fixture()
        self.capture(invocation)
        with mock.patch.object(shutil, "copyfileobj", side_effect=AssertionError("valid copy must be reused")):
            self.assertEqual(self.capture(invocation)["errors"], [])
        (invocation / "reports/test.log").write_text("corrupt")
        (invocation / "reports/test.xml").unlink()
        with mock.patch.object(shutil, "copyfileobj", wraps=shutil.copyfileobj) as copying:
            repaired = self.capture(invocation)
        self.assertEqual(copying.call_count, 2)
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
        value = self.verdict(invocation)
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

    def test_empty_bundle_never_passes(self) -> None:
        value = report.bundle(self.evidence, 19, 0, self.root / "bundle.tar.gz")
        self.assertEqual(value["status"], "INCOMPLETE")
        self.assertEqual(value["invocations"], [])
        self.assertEqual((value["native_exit"], value["runner_exit"]), (None, 19))

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
                responses = [json.dumps(value).encode() for value in (metadata, artifacts)]
                def download(command: list[str], *, stdout: BinaryIO, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
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
                shutil.rmtree(invocation)

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
                with mock.patch.object(report.time, "sleep") as sleep:
                    value = self.native_collect(metadata, [self.metadata_response(pending),
                                                           self.metadata_response(metadata),
                                                           self.native_response(artifact.read_bytes())])
                sleep.assert_called_once()
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
        clock = [0]

        def expire(_: float) -> None:
            clock[0] = 31

        with mock.patch.object(report.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(report.time, "sleep", side_effect=expire):
            value = self.native_collect(metadata, [self.metadata_response(pending)])
        self.assertEqual((value["status"], value["collection_status"]), ("INCOMPLETE", "ERROR"))
        self.assertIn("deadline", value["collection_error"])
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
