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
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock

import qualification_report as report


FIXTURES = Path(__file__).with_name("testdata") / "qualification_report"


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

    def capture(self, invocation: Path) -> dict[str, object]:
        return report.capture(invocation / "events.jsonl", invocation / "captured.jsonl",
                              invocation, invocation / "reports")

    def verdict(self, invocation: Path, *, analysis: Path | None = None, benchmarks: Path | None = None) -> dict[str, object]:
        events = invocation / "captured.jsonl"
        value = report.verdict(events, report.manifest_path(events), invocation / "context.json",
                               invocation / "expected.txt", (analysis,) if analysis else (), benchmarks)
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
