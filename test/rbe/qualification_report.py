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

"""Retain and reconcile one qualification invocation, then bundle its evidence.

The caller owns selection, source checkout and process execution. This module
reads those original records; native-receipt collects an already submitted
BuildBuddy invocation without submitting or rerunning workloads.
Context paths are relative to the context JSON, or absolute. The context names
expected_source, commit_file, source_before, source_after, final_head,
executed_arguments, native_exit, capture_exit, execution_finished_at (UTC), and
command_kind (test, benchmark, build or analysis). Optional lock_before/lock_after must be
supplied together. Exit values are read from files, not inferred from reports.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import datetime
import hashlib
from http.client import HTTPException, HTTPResponse, HTTPSConnection
import json
import math
import os
from pathlib import Path
import re
import shutil
import tarfile
import time
from typing import BinaryIO
import uuid
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

import unit_matrix


MAX_REPORT_BYTES = 512 * 1024 * 1024
MAX_TOTAL_REPORT_BYTES = 2 * 1024 * 1024 * 1024
RUNTIME_OUTPUT_NAME = r"batch-\d+(?:-sentry-stacks)?\.log"
RUNTIME_RESULT_TAGS = {"jtreg-results", "nodejs-results"}
EVENT_KEYS = {
    "id", "children", "started", "finished", "configured", "completed",
    "testResult", "testSummary", "aborted", "namedSetOfFiles",
}


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Expected an object with string keys")
    return value


def string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Expected a string, got {value!r}")
    return value


def sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("Expected a list")
    return value


def number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"Expected an integer, got {value!r}")
    return int(value)


def read_json(path: Path) -> dict[str, object]:
    return mapping(json.loads(path.read_text()))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Receipt collection exceeded its deadline; attach again to resume")


class DeadlineReader:
    """Check the deadline around each bounded file or archive input read."""

    def __init__(self, stream: BinaryIO, deadline: float | None):
        self.stream = stream
        self.deadline = deadline

    def read(self, size: int) -> bytes:
        check_deadline(self.deadline)
        chunk = self.stream.read(size)
        check_deadline(self.deadline)
        return chunk


def file_record(path: Path, *, deadline: float | None = None) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        reader = DeadlineReader(stream, deadline)
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(chunk)
    check_deadline(deadline)
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def manifest_path(events: Path) -> Path:
    return Path(str(events) + ".manifest.json")


def timestamp(value: str) -> datetime.datetime:
    """Read RFC3339 coordinator/BEP times with Python 3.8's ISO parser."""
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})", value)
    if match is None:
        raise ValueError(f"Expected a timezone-aware timestamp: {value}")
    seconds, fraction, zone = match.groups()
    if fraction is not None:
        seconds += "." + (fraction + "000000")[:6]
    return datetime.datetime.fromisoformat(seconds + ("+00:00" if zone == "Z" else zone))


@dataclass(frozen=True, order=True)
class Owner:
    label: str
    configuration: str

    @classmethod
    def parse(cls, raw: object) -> Owner:
        value = mapping(raw)
        label = string(value.get("label"))
        configuration = string(mapping(value.get("configuration")).get("id"))
        if not label.startswith(("//", "@")) or not configuration:
            raise ValueError(f"Invalid configured owner: {value}")
        return cls(label, configuration)


@dataclass(frozen=True, order=True)
class Aspect:
    owner: Owner
    name: str

    @classmethod
    def parse(cls, raw: object) -> Aspect:
        value = mapping(raw)
        return cls(Owner.parse(value), string(value.get("aspect")))


@dataclass(frozen=True, order=True)
class Attempt:
    owner: Owner
    run: int
    shard: int
    attempt: int

    @classmethod
    def parse(cls, raw: object) -> Attempt:
        value = mapping(raw)
        result = cls(Owner.parse(value), *(number(value.get(key, 1)) for key in ("run", "shard", "attempt")))
        if min(result.run, result.shard, result.attempt) < 1:
            raise ValueError(f"Invalid test attempt: {value}")
        return result


def declared_output_groups(targets: dict[str, unit_matrix.ConfiguredTarget]) -> set[tuple[Owner, str]]:
    """Read the small report output groups explicitly requested by each owner."""
    groups = set()
    for label, target in targets.items():
        for tag in target.tags:
            if not tag.startswith("qualification-output-group="):
                continue
            group = tag.removeprefix("qualification-output-group=")
            if re.fullmatch(r"[A-Za-z0-9_]+", group) is None:
                raise ValueError(f"Invalid qualification output group: {tag}")
            groups.add((Owner(label, target.configuration), group))
    return groups


def advertised_build_outputs(events: Path, groups: set[tuple[Owner, str]]) -> list[dict[str, object]]:
    """Resolve declared groups through BEP file sets, independent of event order."""
    if not groups:
        return []
    sets = {}
    selected = {}
    with events.open() as stream:
        for line in stream:
            event = mapping(json.loads(line))
            identity = mapping(event.get("id"))
            if "namedSetOfFiles" in event:
                name = string(mapping(identity.get("namedSet")).get("id"))
                if name in sets:
                    raise ValueError(f"Duplicate build output file set: {name}")
                sets[name] = mapping(event["namedSetOfFiles"])
            if "completed" not in event or mapping(identity.get("targetCompleted")).get("aspect"):
                continue
            owner = Owner.parse(identity["targetCompleted"])
            for raw in sequence(mapping(event["completed"]).get("outputGroup", [])):
                group = mapping(raw)
                key = owner, string(group.get("name"))
                if key not in groups:
                    continue
                if key in selected or group.get("incomplete", False):
                    raise ValueError(f"Duplicate or incomplete build output group: {key}")
                selected[key] = group
    if groups != selected.keys():
        raise ValueError(f"Missing build output groups: {sorted(groups - selected.keys())}")

    def files(name: str, ancestors: frozenset[str]) -> Iterator[dict[str, object]]:
        if name in ancestors or name not in sets:
            raise ValueError(f"Cyclic or missing build output file set: {name}")
        value = sets[name]
        yield from (mapping(item) for item in sequence(value.get("files", [])))
        for child in sequence(value.get("fileSets", [])):
            yield from files(string(mapping(child).get("id")), ancestors | {name})

    outputs = []
    for (owner, group), value in sorted(selected.items()):
        found = {}
        for raw in sequence(value.get("fileSets", [])):
            for item in files(string(mapping(raw).get("id")), frozenset()):
                name = "/".join([string(part) for part in sequence(item.get("pathPrefix", []))] + [string(item.get("name"))])
                if name in found and found[name] != item:
                    raise ValueError(f"Conflicting build output: {name}")
                found[name] = item
        if not found:
            raise ValueError(f"Empty build output group: {owner} {group}")
        for name, item in sorted(found.items()):
            digest, size = string(item.get("digest")), number(item.get("length"))
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None or not 0 <= size <= MAX_REPORT_BYTES:
                raise ValueError(f"Invalid build output digest or size: {name}")
            outputs.append({"identity": {"label": owner.label, "configuration": {"id": owner.configuration}},
                            "group": group, "name": name, "uri": string(item.get("uri")), "sha256": digest, "bytes": size})
    return outputs


def capture(events: Path, output: Path, output_root: Path, report_dir: Path, retain_outputs: bool = False,
            expected_analysis: tuple[Path, ...] = ()) -> dict[str, object]:
    """Copy all advertised original reports before returning capture failures."""
    errors: list[str] = []
    reports: list[dict[str, object]] = []
    runtime_labels: set[str] = set()
    output_root = output_root.resolve()
    if events.resolve() == output.resolve():
        raise ValueError("Raw and sanitized event files must differ")
    report_dir.resolve().relative_to(output.parent.resolve())
    report_dir.mkdir(parents=True, exist_ok=True)
    report_dir = report_dir.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    count = total = 0
    seen: set[tuple[Attempt, str]] = set()
    previous: dict[tuple[Attempt, str], dict[str, object]] = {}
    input_events: dict[str, object] = {}

    def retain(uri_text: str, expected: dict[str, object] | None = None,
               saved: dict[str, object] | None = None) -> dict[str, object]:
        nonlocal total
        uri = urlsplit(uri_text)
        if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
            raise ValueError(f"Expected a local report URI: {uri_text}")
        path = Path(unquote(uri.path)).resolve()
        relative = path.relative_to(output_root)
        destination = (report_dir / relative).resolve()
        destination.relative_to(report_dir)
        reusable = expected
        if reusable is None and saved is not None and saved.get("path") == str(destination.relative_to(output.parent.resolve())):
            reusable = {"sha256": saved.get("sha256"), "bytes": saved.get("bytes")}
        if reusable is not None and destination.is_file() and file_record(destination) == reusable:
            record = reusable
            total += number(record["bytes"])
            if total > MAX_TOTAL_REPORT_BYTES:
                raise ValueError("Retained reports exceed total size bound")
        else:
            size = path.stat().st_size
            if not path.is_file() or size > MAX_REPORT_BYTES or total + size > MAX_TOTAL_REPORT_BYTES:
                raise ValueError(f"Report size/type exceeds retention bounds: {path}")
            if expected is not None and size != expected["bytes"]:
                raise ValueError(f"Report differs from its advertised digest/size: {path}")
            total += size
            destination.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as src, destination.open("wb") as dst:
                remaining = size
                while remaining:
                    chunk = src.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise ValueError(f"Report shrank during capture: {path}")
                    dst.write(chunk)
                    remaining -= len(chunk)
                if src.read(1):
                    raise ValueError(f"Report grew during capture: {path}")
            record = file_record(destination)
            if record["bytes"] != size or expected is not None and record != expected:
                raise ValueError(f"Report differs from its advertised digest/size: {path}")
        return {"path": str(destination.relative_to(output.parent.resolve())), **record}

    try:
        input_events = file_record(events)
        if manifest_path(output).exists():
            saved = read_json(manifest_path(output))
            if saved.get("input_events") == input_events:
                previous = {(Attempt.parse(mapping(row).get("identity")), string(mapping(row).get("name"))): mapping(row)
                            for row in sequence(saved.get("reports"))}
    except (OSError, ValueError):
        # A missing/corrupt earlier manifest cannot authenticate reusable copies.
        previous = {}
    try:
        # BEP children may precede their parents. Discover output contracts
        # before copying any result, regardless of the event ordering.
        with events.open() as source:
            for line in source:
                try:
                    event = mapping(json.loads(line))
                    if RUNTIME_RESULT_TAGS.intersection(mapping(event.get("configured", {})).get("tag", [])):
                        identity = mapping(event.get("id"))
                        runtime_labels.add(string(mapping(identity.get("targetConfigured")).get("label")))
                except (ValueError, TypeError):
                    # The capture pass below reports malformed events.
                    continue
        with events.open() as source, output.open("w") as target:
            for line_number, line in enumerate(source, 1):
                try:
                    event = mapping(json.loads(line))
                    identity = mapping(event.get("id"))
                    if "testResult" in event:
                        attempt = Attempt.parse(identity.get("testResult"))
                        result = mapping(event["testResult"])
                        advertised: dict[str, list[dict[str, object]]] = {}
                        for item in sequence(result.get("testActionOutput", [])):
                            report = mapping(item)
                            advertised.setdefault(string(report.get("name")), []).append(report)
                        names = ["test.log", "test.xml"]
                        # Guest logs can survive only in undeclared outputs when
                        # the wrapper times out before printing them to test.log.
                        # Bazel may publish a ZIP or the individual files.
                        if retain_outputs or result.get("status") != "PASSED":
                            names.extend(name for name in advertised
                                         if name == "test.outputs__outputs.zip" or name.startswith("test.outputs/"))
                        elif attempt.owner.label in runtime_labels:
                            names.extend(name for name in advertised
                                         if name == "test.outputs__outputs.zip" or re.fullmatch("test.outputs/" + RUNTIME_OUTPUT_NAME, name))
                        for name in names:
                            try:
                                matches = advertised.get(name, [])
                                if len(matches) != 1 or (attempt, name) in seen:
                                    raise ValueError(f"Expected one original {name} for {attempt}")
                                seen.add((attempt, name))
                                (report,) = matches
                                old = previous.get((attempt, name))
                                record = retain(string(report.get("uri")), saved=old)
                                reports.append({"identity": identity["testResult"], "name": name, **record})
                            except (OSError, ValueError) as error:
                                errors.append(f"line {line_number}: {error}")
                    if "started" in event:
                        started = mapping(event["started"])
                        event["started"] = {key: started[key] for key in ("uuid", "startTime", "startTimeMillis", "command") if key in started}
                    serialized = json.dumps({key: value for key, value in event.items() if key in EVENT_KEYS})
                    if "x-buildbuddy-api-key" in serialized.lower():
                        raise ValueError("Credential marker in retained event")
                    target.write(serialized + "\n")
                    count += 1
                except (ValueError, TypeError) as error:
                    errors.append(f"line {line_number}: {error}")
    except OSError as error:
        errors.append(str(error))
    if not output.exists():
        output.write_text("")
    if count == 0:
        errors.append("No complete build events")
    build_outputs = []
    try:
        groups = set()
        for analysis in expected_analysis:
            groups.update(declared_output_groups(unit_matrix.configured_targets(str(analysis), tests=None)))
        for item in advertised_build_outputs(output, groups):
            try:
                record = retain(string(item["uri"]), {"sha256": item["sha256"], "bytes": item["bytes"]})
                build_outputs.append({**item, **record})
            except (OSError, ValueError) as error:
                errors.append(str(error))
    except (OSError, ValueError) as error:
        errors.append(str(error))
    result = {"schema_version": 1, "input_events": input_events, "events": file_record(output),
              "reports": reports, "build_outputs": build_outputs, "errors": errors}
    write_json(manifest_path(output), result)
    return result


def xml_cases(path: Path) -> dict[str, object]:
    """Count actual entries, retaining skips/notrun separately from passes."""
    root = ET.parse(path).getroot()
    if root.tag not in ("testsuites", "testsuite"):
        raise ValueError(f"Unexpected XML root: {root.tag}")
    counts: Counter[str] = Counter()
    failures, skips = [], []
    for case in root.iter("testcase"):
        name = "/".join(filter(None, (case.get("classname"), case.get("name"))))
        if case.find("failure") is not None or case.find("error") is not None:
            counts["failed"] += 1
            failures.append(name)
        elif case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed":
            counts["skipped"] += 1
            skips.append({"name": name, "status": case.get("status"), "reason": "".join(case.find("skipped").itertext()) if case.find("skipped") is not None else case.get("result", "notrun")})
        else:
            counts["passed"] += 1
    # Suite failures can exist without a corresponding named testcase.
    suite_errors = sum(number(suite.get(key, 0)) for suite in root.iter("testsuite") for key in ("failures", "errors"))
    if suite_errors and not counts["failed"]:
        raise ValueError("XML declares failures without failed testcase entries")
    declared = root.get("tests")
    if declared is not None and number(declared) != sum(counts.values()):
        raise ValueError(f"XML declares {declared} tests but contains {sum(counts.values())}")
    return {"passed": counts["passed"], "failed": counts["failed"], "skipped": counts["skipped"], "failures": failures, "skips": skips}


def runtime_batches(log: Path, attempt: Attempt, reports: dict[tuple[Attempt, str], Path]
                    ) -> tuple[list[str], list[tuple[list[str], str]], list[str]]:
    """Bind retained batch output to the runner's original selected names."""
    pattern = r"^I\d{4} \S+\s+\d+ lib\.go:\d+\] Running test case batch: (.+)$"
    batches = [match[1].split(",") for line in log.read_text().splitlines()
               if (match := re.fullmatch(pattern, line))]
    expected = [name for batch in batches for name in batch]
    if not expected or len(expected) != len(set(expected)):
        raise ValueError("Runtime selection is empty or contains duplicate names")

    def outputs() -> Iterator[str]:
        total = 0
        if archive_path := reports.get((attempt, "test.outputs__outputs.zip")):
            with zipfile.ZipFile(archive_path) as archive:
                names = [item for item in archive.infolist() if re.fullmatch(r"batch-\d+\.log", item.filename)]
                if len(names) != len({item.filename for item in names}):
                    raise ValueError("Duplicate runtime batch artifacts")
                for item in names:
                    total += item.file_size
                    if item.file_size > MAX_REPORT_BYTES or total > MAX_TOTAL_REPORT_BYTES:
                        raise ValueError("Runtime batch output exceeds retention bounds")
                    yield archive.read(item).decode()
        else:
            for (owner, name), path in reports.items():
                if owner == attempt and re.fullmatch(r"test.outputs/batch-\d+\.log", name):
                    yield path.read_text()

    found_batches = []
    retained = []
    errors = []
    try:
        for output in outputs():
            selection, separator, body = output.partition("\nOutput:\n")
            if not separator or not selection.startswith("Batch:\n"):
                errors.append("Invalid runtime batch artifact header")
                continue
            names = selection.removeprefix("Batch:\n").splitlines()
            found_batches.append(names)
            retained.append((names, body))
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        errors.append(str(error))
    if Counter(map(tuple, batches)) != Counter(map(tuple, found_batches)):
        errors.append("Retained runtime batches differ from the selected batches")
    return expected, retained, errors


def jtreg_results(log: Path, attempt: Attempt, reports: dict[tuple[Attempt, str], Path]) -> dict[str, object]:
    """Reconcile selected Java names with jtreg's retained per-test results."""
    expected, batches, errors = runtime_batches(log, attempt, reports)
    results: dict[str, str] = {}
    for names, body in batches:
        current = None
        awaiting_header = False
        lines = body.splitlines()
        # jtreg's VerboseHandler frames each full result with separators.
        # Section stdout is arbitrary text and can itself start with TEST:.
        # Proctor heartbeats can appear between results, after the shared
        # separator but before the next header.
        separator = "--------------------------------------------------"
        for index, line in enumerate(lines):
            if line == separator and current is None:
                awaiting_header = True
            elif line.startswith("TEST: ") and awaiting_header:
                awaiting_header = False
                current = line.removeprefix("TEST: ")
                if current not in names or current in results:
                    errors.append(f"Unexpected or duplicate Java test: {current}")
                    current = None
            elif (line.startswith("TEST RESULT: ") and index + 1 < len(lines)
                  and lines[index + 1] == separator):
                if current is None:
                    errors.append("Java result has no test identity")
                    continue
                result = line.removeprefix("TEST RESULT: ")
                if not result.startswith(("Passed.", "Failed.", "Error.")):
                    errors.append(f"Unrecognized Java result: {result}")
                else:
                    results[current] = result
                current = None
    skipped = {name: result for name, result in results.items() if result.startswith("Passed. Skipped:")}
    return {"expected": expected, "results": results, "errors": errors,
            "passed": sum(result.startswith("Passed.") for result in results.values()) - len(skipped),
            "skipped": skipped,
            "failures": [name for name, result in results.items() if not result.startswith("Passed.")],
            "missing": sorted(set(expected) - results.keys())}


def nodejs_results(log: Path, attempt: Attempt, reports: dict[tuple[Attempt, str], Path]) -> dict[str, object]:
    """Require completed Node harness TAP results, not its zero exit alone.

    Node's TAP ``ok`` means the configured expected outcome was observed.
    A status-file expected failure can therefore be ``ok`` without raw exit 0.
    TODO annotates expected flakiness without negating a completed ``ok``;
    every ``not ok`` remains a qualification failure, including TODO.
    """
    expected, batches, errors = runtime_batches(log, attempt, reports)
    results: dict[str, str] = {}
    failures: set[str] = set()
    skipped: dict[str, str] = {}
    excluded: dict[str, str] = {}
    expected_flaky: dict[str, str] = {}
    heartbeat = r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} INFO Proctor checking in .+"
    # Require the pinned Node HasRun frame, not arbitrary valid TAP:
    # https://github.com/nodejs/node/blob/d57af10d1/tools/test.py#L338-L413
    for names, body in batches:
        # NormalizePath in the pinned tools/test.py removes the JS extension.
        normalized = {name.removesuffix(".js"): name for name in names}
        if len(normalized) != len(names):
            errors.append("Node selection has ambiguous normalized names")
        header = False
        plan = None
        identifiers: list[int] = []
        current: tuple[str, str, str | None] | None = None
        diagnostic = duration = False
        footer = None
        omitted: dict[str, str] = {}
        for line in body.splitlines():
            if not line or re.fullmatch(heartbeat, line):
                continue
            if footer is not None:
                # Main prints failed command lines after the TAP frames.
                # This tail cannot supply or repair completion evidence.
                if footer != "Failed tests:" or re.match(
                        r"(?:TAP version |1\.\.|(?:not )?ok |# gvisor-node-skip )", line):
                    errors.append(f"Unexpected Node output after footer: {line}")
            elif line.startswith("# gvisor-node-skip "):
                # The image patch reports the canonical should_keep decision;
                # do not reconstruct the status-file classifier here.
                try:
                    match json.loads(line.removeprefix("# gvisor-node-skip ")):
                        case {"name": str(normalized_name), "reason": str(reason), **extra} if (
                                not extra and reason in ("--skip-tests", "status-file SKIP", "--flaky-tests=skip")):
                            name = normalized.get(normalized_name, normalized_name)
                            if header or normalized_name not in normalized or name in omitted or name in results:
                                raise ValueError(f"Unexpected or duplicate Node exclusion: {name}")
                            omitted[name] = reason
                        case _:
                            raise ValueError("Invalid Node exclusion record")
                except ValueError as error:
                    errors.append(str(error))
            elif line in ("All tests passed.", "Failed tests:"):
                # The footer is unconditional on successful HasRun accounting:
                # Main can print it even after RunSingle swallowed an IOError.
                if (current is not None or plan is None or len(identifiers) != plan
                        or not set(names) <= results.keys() | omitted.keys()):
                    errors.append("Node footer precedes complete result accounting")
                if line == "Failed tests:" and not failures.intersection(names):
                    errors.append("Node failure footer has no failed result")
                footer = line
            elif line == "No tests to run." and not header and len(omitted) == len(names):
                footer = line
            elif line == "TAP version 13":
                if header or current is not None:
                    errors.append("Unexpected Node TAP header")
                header = True
            elif match := re.fullmatch(r"1\.\.(\d+)", line):
                if not header or plan is not None or current is not None:
                    errors.append("Unexpected Node TAP plan")
                plan = int(match[1])
            elif match := re.fullmatch(r"(ok|not ok) (\d+) (\S+?)(?: # (.+))?", line):
                outcome, identifier, normalized_name, directive = match.groups()
                name = normalized.get(normalized_name, normalized_name)
                if outcome == "not ok":
                    # Preserve observed failures even if a later frame is lost.
                    failures.add(name)
                if not header or plan is None or current is not None:
                    errors.append("Node result is outside a complete TAP frame")
                identifiers.append(int(identifier))
                if normalized_name not in normalized or name in results or name in omitted:
                    errors.append(f"Unexpected or duplicate Node test: {name}")
                if directive and not re.match(r"(?:skip|todo)(?:\s|:|$)", directive, re.IGNORECASE):
                    errors.append(f"Unknown Node TAP directive: {directive}")
                current = (name, outcome, directive)
                diagnostic = duration = False
            elif line == "  ---" and current is not None and not diagnostic:
                diagnostic = True
            elif match := re.fullmatch(r"  duration_ms: (\d+(?:\.\d+)?)", line):
                if current is None or not diagnostic or duration:
                    errors.append("Unexpected Node TAP duration")
                duration = True
            elif line == "  ..." and current is not None and diagnostic:
                name, outcome, directive = current
                if not duration:
                    errors.append(f"Node result has no duration: {name}")
                elif name not in results and name in names:
                    results[name] = outcome
                    if directive and re.match(r"skip(?:\s|:|$)", directive, re.IGNORECASE):
                        skipped[name] = directive
                    elif directive and re.match(r"todo(?:\s|:|$)", directive, re.IGNORECASE):
                        expected_flaky[name] = directive
                current = None
                diagnostic = duration = False
            elif diagnostic and line.startswith("  "):
                # The emitter indents child stdout/stderr by four spaces;
                # it cannot create a top-level TAP result or close this frame.
                continue
            else:
                errors.append(f"Unexpected Node TAP output: {line}")
        count = len(names) - len(omitted)
        if footer != "No tests to run." and (
                not header or plan != count or identifiers != list(range(1, count + 1))):
            errors.append("Node TAP plan/results do not cover the selected batch")
        if current is not None:
            errors.append(f"Unfinished Node TAP result: {current[0]}")
        excluded.update(omitted)
        skipped.update(omitted)
    return {"expected": expected, "results": results, "errors": errors,
            "passed": sum(result == "ok" and name not in skipped for name, result in results.items()),
            "skipped": skipped, "excluded": excluded, "expected_flaky": expected_flaky, "failures": sorted(failures),
            "missing": sorted(set(expected) - results.keys() - excluded.keys())}


@dataclass(frozen=True)
class Benchmark:
    name: str
    count: int
    units: tuple[str, ...]
    iterations: int | None


def benchmark_manifest(path: Path) -> tuple[list[str], dict[str, list[Benchmark]]]:
    """Read the explicit benchmark-only Go invocation contract, before Bazel."""
    value = read_json(path)
    pgo = "pgo" in value
    if set(value) != {"arguments", "owners"} | ({"pgo"} if pgo else set()):
        raise ValueError("Benchmark manifest requires exactly arguments and owners")
    arguments = [string(item) for item in sequence(value.get("arguments"))]
    flags: dict[str, str] = {}
    for argument in arguments:
        match = re.fullmatch(r"-test\.(run|bench|v|benchtime|count|cpu|benchmem)(?:=([^\r\n]+))?", argument)
        if match is None:
            raise ValueError(f"Unsupported benchmark argument: {argument}")
        name, setting = match.groups()
        if name in flags or setting is None and name not in ("benchmem", "v"):
            raise ValueError(f"Duplicate or missing benchmark flag value: {argument}")
        flags[name] = "true" if setting is None else setting
    if pgo and flags != {"v": "true", "bench": ".", "benchtime": "20s"}:
        raise ValueError("PGO requires the public verbose, whole-benchmark, 20-second arguments")
    if not pgo and (flags.get("run") != "^$" or flags.get("v") != "false" or not flags.get("bench")):
        raise ValueError("Benchmark arguments require -test.run=^$, -test.v=false and -test.bench=PATTERN")
    owners = benchmark_expectations(value.get("owners"))
    if pgo:
        profiles = pgo_expectations(value)
        if set(profiles) != set(owners):
            raise ValueError("PGO exclusions must name every selected owner")
        for owner, leaves in owners.items():
            if set(profiles[owner].excluded) & {leaf.name for leaf in leaves}:
                raise ValueError(f"PGO active and excluded leaves overlap for {owner}")
    return arguments, owners


def benchmark_expectations(value: object) -> dict[str, list[Benchmark]]:
    """Read exact measurement obligations shared by both benchmark routes."""
    owners: dict[str, list[Benchmark]] = {}
    for label, rows in mapping(value).items():
        if not label.startswith(("//", "@")):
            raise ValueError(f"Invalid benchmark owner: {label}")
        leaves = []
        for raw in sequence(rows):
            row = mapping(raw)
            if set(row) - {"name", "count", "units", "iterations"}:
                raise ValueError(f"Unknown benchmark expectation fields: {row}")
            name = string(row.get("name"))
            count = number(row.get("count"))
            units = tuple(string(unit) for unit in sequence(row.get("units")))
            iterations = number(row["iterations"]) if "iterations" in row else None
            if (not name.startswith("Benchmark") or any(char.isspace() for char in name) or count < 1
                    or not units or len(set(units)) != len(units) or any(not unit or any(c.isspace() for c in unit) for unit in units)
                    or iterations is not None and iterations < 1):
                raise ValueError(f"Invalid benchmark expectation for {label}: {row}")
            leaves.append(Benchmark(name, count, units, iterations))
        if not leaves or len({leaf.name for leaf in leaves}) != len(leaves):
            raise ValueError(f"Expected unique nonempty benchmark leaves for {label}")
        owners[label] = leaves
    if not owners:
        raise ValueError("No expected benchmark owners")
    return owners


@dataclass(frozen=True)
class PGO:
    excluded: tuple[str, ...]
    platform: str
    benchmark: str


def pgo_expectations(value: dict[str, object]) -> dict[str, PGO]:
    """Read source-selected profile identities and deliberate PGO exclusions."""
    profiles = {}
    for owner, raw in mapping(value["pgo"]).items():
        row = mapping(raw)
        if set(row) != {"excluded", "platform", "benchmark"}:
            raise ValueError(f"Invalid PGO fields for {owner}")
        leaves = tuple(string(name) for name in sequence(row["excluded"]))
        platform, benchmark = string(row["platform"]), string(row["benchmark"])
        if (len(set(leaves)) != len(leaves)
                or any(not name.startswith("Benchmark") or any(c.isspace() for c in name) for name in leaves)
                or platform not in ("systrap", "kvm") or re.fullmatch(r"[a-z0-9_]+", benchmark) is None):
            raise ValueError(f"Invalid PGO expectation for {owner}")
        profiles[owner] = PGO(leaves, platform, benchmark)
    return profiles


def pgo_result(attempt: Attempt, reports: dict[tuple[Attempt, str], Path], expected: PGO) -> dict[str, object]:
    """Verify the generated profile and refresh decision from retained outputs."""
    names = ("pgo/result.json", f"pgo/{expected.benchmark}.pgo.pprof.pb.gz")
    if archive_path := reports.get((attempt, "test.outputs__outputs.zip")):
        with zipfile.ZipFile(archive_path) as archive:
            for name in names:
                if archive.namelist().count(name) != 1 or archive.getinfo(name).file_size > MAX_REPORT_BYTES:
                    raise ValueError(f"Expected one bounded PGO output: {name}")
            metadata, profile = (archive.read(name) for name in names)
    else:
        metadata, profile = (reports[(attempt, "test.outputs/" + name)].read_bytes() for name in names)
    value = mapping(json.loads(metadata))
    fields = {"runtime_key", "platform", "benchmark", "generated_sha256", "cpu_samples", "decision", "comparison"}
    if set(value) not in (fields, fields | {"baseline_sha256"}):
        raise ValueError("Invalid PGO result fields")
    if (value["platform"] != expected.platform or value["benchmark"] != expected.benchmark
            or not string(value["runtime_key"]).endswith("-linux-amd64") or number(value["cpu_samples"]) <= 0
            or not profile or hashlib.sha256(profile).hexdigest() != value["generated_sha256"]):
        raise ValueError("PGO profile identity, CPU samples or digest differ")
    decision, comparison = string(value["decision"]), string(value["comparison"])
    if decision == "add":
        if "baseline_sha256" in value or comparison:
            raise ValueError("New PGO baseline unexpectedly has a comparison")
    elif decision in ("retain", "refresh"):
        if not comparison or re.fullmatch(r"[0-9a-f]{64}", string(value.get("baseline_sha256"))) is None:
            raise ValueError("PGO comparison requires the existing baseline digest and result")
    else:
        raise ValueError(f"Invalid PGO refresh decision: {decision}")
    return {"generation": "PASS", **value, "bytes": len(profile)}


def benchmark_rows(path: Path, pgo: bool = False) -> list[dict[str, object]]:
    """Read Go's benchmark name/iteration/(value unit)+ output, not test events.

    Accept finite numeric metrics; neither aggregate nor compare values.
    https://pkg.go.dev/golang.org/x/perf/benchfmt#Reader
    """
    rows = []
    completed = False
    with path.open() as stream:
        for line in stream:
            text = line.removeprefix("\x16").strip()
            if text == "PASS":
                if completed:
                    raise ValueError("Benchmark log has multiple Go package PASS markers")
                completed = True
                continue
            if text == "FAIL":
                raise ValueError("Benchmark log has a Go package FAIL marker")
            fields = text.split()
            if not fields or not fields[0].startswith("Benchmark") or len(fields) == 1:
                continue
            if completed:
                raise ValueError("Benchmark measurement follows the Go package PASS marker")
            name, iterations, *measurements = fields
            if not iterations.isdecimal() or int(iterations) < 1 or not measurements or len(measurements) % 2:
                raise ValueError(f"Malformed benchmark row: {text}")
            metrics: dict[str, float] = {}
            for raw, unit in zip(measurements[::2], measurements[1::2]):
                value = float(raw)
                if not math.isfinite(value) or unit in metrics:
                    raise ValueError(f"Invalid or duplicate benchmark metric: {text}")
                metrics[unit] = value
            rows.append({"name": re.sub(r"-\d+$", "", name) if pgo else name,
                         "iterations": int(iterations), "metrics": metrics})
    # Bazel appends spawn stderr after stdout, including Docker image-pull logs.
    # https://github.com/bazelbuild/bazel/blob/f8278f94e/src/main/java/com/google/devtools/build/lib/exec/StandaloneTestStrategy.java#L835-L839
    if not completed:
        raise ValueError("Benchmark log has no Go package PASS marker")
    return rows


def source_context(path: Path) -> tuple[dict[str, object], list[str]]:
    context = read_json(path)
    errors: list[str] = []
    evidence: dict[str, object] = {}
    bodies: dict[str, bytes] = {}
    for key in ("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit", "lock_before", "lock_after"):
        if key.startswith("lock_") and key not in context:
            continue
        try:
            original = path.parent / string(context.get(key))
            bodies[key] = original.read_bytes()
            evidence[key] = {"path": str(original), **file_record(original)}
        except (OSError, ValueError) as error:
            errors.append(f"{key}: {error}")
    source = string(context.get("expected_source"))
    if re.fullmatch(r"[0-9a-f]{40}", source) is None:
        errors.append("Invalid expected source commit")
    if "commit_file" in bodies:
        body = bodies["commit_file"]
        actual = hashlib.sha1(b"commit " + str(len(body)).encode() + b"\0" + body).hexdigest()
        if actual != source:
            errors.append(f"Commit body identifies {actual}, expected {source}")
    for key in ("source_before", "source_after"):
        if bodies.get(key, b"").strip():
            errors.append(f"Tracked source is dirty: {key}")
    if bodies.get("final_head", b"").decode().strip() != source:
        errors.append("Final source HEAD differs from expected commit")
    if ("lock_before" in context) != ("lock_after" in context) or bodies.get("lock_before") != bodies.get("lock_after"):
        errors.append("Lock evidence is missing or changed")
    kind = context.get("command_kind")
    arguments = []
    if kind == "command":
        try:
            match json.loads(bodies.get("executed_arguments", b"")):
                case list() as values if all(isinstance(value, str) for value in values):
                    arguments = values
                case _:
                    raise ValueError("Command arguments must be a JSON string array")
        except ValueError as error:
            errors.append(f"executed_arguments: {error}")
    else:
        arguments = bodies.get("executed_arguments", b"").decode().splitlines()
    bazel_arguments = arguments[:arguments.index("--")] if "--" in arguments else arguments
    analysis = any(arg in ("--nobuild", "--nobuild=true", "--build=false") for arg in bazel_arguments)
    if kind not in ("test", "benchmark", "build", "analysis", "command") or kind != "command" and (kind == "analysis") != analysis:
        errors.append("Command kind does not match original analysis/execution arguments")
    command = next((arg for arg in arguments if not arg.startswith("-")), None)
    allowed_commands = ("run", "query") if kind == "command" else ("test", "build")
    if command not in allowed_commands or kind in ("test", "build") and command != kind:
        errors.append("Missing or inconsistent original Bazel command")
    if kind == "benchmark" and command != "test":
        errors.append("Benchmark evidence requires a Bazel test command")
    if kind == "command" and command == "run" and analysis:
        errors.append("Analysis-only run cannot establish command execution")
    exits = {}
    for key in ("native_exit", "capture_exit"):
        try:
            exits[key] = int(bodies[key].decode().strip())
            if kind == "command" and not 0 <= exits[key] <= 255:
                raise ValueError("Invalid shell exit status")
        except (KeyError, ValueError):
            errors.append(f"Missing or invalid {key}")
            exits[key] = None if kind == "command" else 1
    finished = None
    try:
        value = timestamp(string(context.get("execution_finished_at")))
        if value.utcoffset() != datetime.timedelta(0):
            raise ValueError("execution_finished_at must be UTC")
        finished = value.isoformat()
    except ValueError as error:
        errors.append(f"execution_finished_at: {error}")
    return {"source": source, "source_evidence": evidence, "command_kind": kind, "bazel_command": command,
            "arguments": arguments, "execution_finished_at": finished, **exits}, errors


def command_verdict(context: Path) -> dict[str, object]:
    """Retain run/query completion without inventing build or test owners."""
    started = time.monotonic()
    info, errors = source_context(context)
    if info["command_kind"] != "command":
        errors.append("Command evidence requires a run/query source context")
    outputs = {}
    for name in ("stdout", "stderr"):
        path = context.parent / (name + ".log")
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Missing regular command output: {name}")
            if path.stat().st_size > MAX_REPORT_BYTES:
                raise ValueError(f"Command output exceeds report size bound: {name}")
            outputs[name] = {"path": path.name, **file_record(path)}
        except (OSError, ValueError) as error:
            errors.append(str(error))
    if info["capture_exit"] != 0:
        errors.append("Caller recorded a capture failure")
    # An absent exit record cannot establish execution failure or success.
    status = "FAIL" if info["native_exit"] else "INCOMPLETE" if errors else "PASS"
    now = datetime.datetime.now(datetime.timezone.utc)
    return {"schema_version": 1, **info, "status": status, "accepted_analysis_exit": False,
            "errors": errors, "command_outputs": outputs, "expected": [], "executed": [],
            "case_counts": {}, "bep_finish": None, "verdict_available_at": now.isoformat(),
            "reporting_elapsed_seconds": time.monotonic() - started,
            "execution_to_verdict_seconds": (now - timestamp(string(info["execution_finished_at"]))).total_seconds()
            if info["execution_finished_at"] is not None else None}


def preflight(events: Path, native_exit: int, kind: str) -> dict[str, object]:
    """Validate analysis before allowing the caller to start expensive actions."""
    errors: list[str] = []
    targets: dict[str, unit_matrix.ConfiguredTarget] = {}
    output_options: list[str] = []
    try:
        if kind not in ("test", "build") or native_exit not in ((0, 1) if kind == "test" else (0,)):
            raise ValueError(f"Unexpected {kind} preflight exit: {native_exit}")
        targets = unit_matrix.configured_targets(
            str(events), tests=True if kind == "test" else None,
            allow_test_no_build=kind == "test", expected_exit=native_exit,
        )
        groups = declared_output_groups(targets)
        if groups:
            output_options = ["--output_groups=+" + ",+".join(sorted({group for _, group in groups})),
                              "--remote_download_outputs=toplevel"]
        if kind == "test":
            paths = []
            for label, target in sorted(targets.items()):
                if not RUNTIME_RESULT_TAGS.intersection(target.tags):
                    continue
                repository, separator, local = label.partition("//")
                if not separator or ":" not in local:
                    raise ValueError(f"Invalid runtime owner label: {label}")
                path = local.replace(":", "/", 1).lstrip("/")
                if repository:
                    path = "external/" + repository.lstrip("@") + "/" + path
                paths.append(re.escape(path))
            if paths:
                # Minimal remote downloads omit passing undeclared outputs.
                # Materialize only these owners' batch logs, not runtime images
                # or successful debug logs. Repeated regex options are additive.
                pattern = r".*/testlogs/(?:" + "|".join(paths) + r")/(?:[^/]+/)*test\.outputs/" + RUNTIME_OUTPUT_NAME + "$"
                output_options.extend(["--nozip_undeclared_test_outputs", "--remote_download_regex=" + pattern])
    except (OSError, ValueError) as error:
        errors.append(str(error))
    return {"schema_version": 1, "status": "INCOMPLETE" if errors else "PASS",
            "kind": kind, "native_exit": native_exit, "errors": errors,
            "output_options": output_options,
            "expected": [asdict(Owner(label, target.configuration)) for label, target in sorted(targets.items())]}


def marshal_test_contract(log: Path) -> dict[str, object]:
    """Read the generator's runtime applicability record from an original log."""
    prefix = "gvisor-go-marshal-test: "
    with log.open() as stream:
        records = [line.removeprefix("\x16")[len(prefix):] for line in stream
                   if line.removeprefix("\x16").startswith(prefix)]
    if len(records) != 1:
        raise ValueError("Generated ABI test requires exactly one applicability record")
    value = mapping(json.loads(records[0]))
    if (set(value) != {"schema_version", "runtime_types", "constraints_match"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["runtime_types"]) is not int or value["runtime_types"] < 0
            or type(value["constraints_match"]) is not bool):
        raise ValueError("Invalid generated ABI test applicability record")
    return value


def verdict(events: Path, manifest: Path, context: Path, expected_targets: Path | None = None,
            expected_analysis: tuple[Path, ...] = (), expected_benchmarks: Path | None = None,
            declared_benchmarks: Path | None = None) -> dict[str, object]:
    """Reconcile configured owners and announced runs/shards with originals."""
    started = time.monotonic()
    errors: list[str] = []
    info, source_errors = source_context(context)
    errors.extend(source_errors)
    benchmark_mode = info["command_kind"] == "benchmark"
    testing = info["command_kind"] in ("test", "benchmark")
    benchmark_expected: dict[str, list[Benchmark]] = {}
    benchmark_default_cpu = True
    benchmark_evidence: dict[str, object] = {}
    pgo_expected: dict[str, PGO] | None = None
    if benchmark_mode != (expected_benchmarks is not None):
        errors.append("Benchmark mode requires its explicit manifest; ordinary commands cannot use one")
    if expected_benchmarks is not None:
        try:
            arguments, benchmark_expected = benchmark_manifest(expected_benchmarks)
            benchmark_default_cpu = not any(argument.startswith("-test.cpu=") for argument in arguments)
            raw_manifest = read_json(expected_benchmarks)
            if "pgo" in raw_manifest:
                pgo_expected = pgo_expectations(raw_manifest)
            benchmark_evidence = {"path": str(expected_benchmarks), **file_record(expected_benchmarks),
                                  "owners": {label: [asdict(leaf) for leaf in leaves] for label, leaves in benchmark_expected.items()}}
            if pgo_expected is not None:
                benchmark_evidence["pgo"] = {label: asdict(profile) for label, profile in pgo_expected.items()}
            original = [arg[len("--test_arg="):] for arg in info["arguments"] if arg.startswith("--test_arg=")]
            if original != arguments:
                errors.append("Original benchmark arguments differ from the manifest")
        except (OSError, ValueError) as error:
            errors.append(f"Benchmark manifest: {error}")
    declared_expected: dict[str, list[Benchmark]] = {}
    declared_evidence: dict[str, object] = {}
    if declared_benchmarks is not None:
        try:
            if info["command_kind"] != "test":
                raise ValueError("Declared benchmark expectations require a mixed test invocation")
            value = read_json(declared_benchmarks)
            if set(value) != {"owners"}:
                raise ValueError("Declared benchmark manifest requires exactly owners")
            declared_expected = benchmark_expectations(value["owners"])
            declared_evidence = {"path": str(declared_benchmarks), **file_record(declared_benchmarks)}
        except (OSError, ValueError) as error:
            errors.append(f"Declared benchmark manifest: {error}")
    retained = read_json(manifest)
    errors.extend(string(item) for item in sequence(retained.get("errors")))
    if retained.get("events") != file_record(events):
        errors.append("Sanitized BEP digest/size differs from capture manifest")
    reports: dict[tuple[Attempt, str], Path] = {}
    for raw in sequence(retained.get("reports")):
        try:
            row = mapping(raw)
            key = Attempt.parse(row.get("identity")), string(row.get("name"))
            report = (manifest.parent / string(row.get("path"))).resolve()
            report.relative_to(manifest.parent.resolve())
            if key in reports or file_record(report) != {"sha256": row.get("sha256"), "bytes": row.get("bytes")}:
                raise ValueError(f"Duplicate or changed original report: {key}")
            reports[key] = report
        except (OSError, ValueError) as error:
            errors.append(str(error))
    configured: set[Owner] = set()
    announced: set[Attempt] = set()
    completed: dict[Owner, bool] = {}
    aspects: set[Aspect] = set()
    completed_aspects: dict[Aspect, bool] = {}
    results: dict[Attempt, dict[str, object]] = {}
    summaries: dict[Owner, dict[str, object]] = {}
    finished: dict[str, object] = {}
    skipped_owners: set[Owner] = set()
    jtreg_tests: set[Owner] = set()
    nodejs_tests: set[Owner] = set()
    with events.open() as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                event = mapping(json.loads(line))
                identity = mapping(event.get("id"))
                children = [mapping(child) for child in sequence(event.get("children", []))]
                if "configured" in event:
                    details = mapping(event["configured"])
                    if details.get("targetKind") != "test_suite rule" and (not testing or "testSize" in details):
                        configured.update(Owner.parse(child["targetCompleted"]) for child in children
                                          if "targetCompleted" in child and not mapping(child["targetCompleted"]).get("aspect"))
                    if "jtreg-results" in details.get("tag", []):
                        jtreg_tests.update(Owner.parse(child["targetCompleted"]) for child in children
                                           if "targetCompleted" in child and not mapping(child["targetCompleted"]).get("aspect"))
                    if "nodejs-results" in details.get("tag", []):
                        nodejs_tests.update(Owner.parse(child["targetCompleted"]) for child in children
                                           if "targetCompleted" in child and not mapping(child["targetCompleted"]).get("aspect"))
                for child in children:
                    if "targetCompleted" in child and mapping(child["targetCompleted"]).get("aspect"):
                        aspects.add(Aspect.parse(child["targetCompleted"]))
                if "aborted" in event and mapping(event["aborted"]).get("reason") == "SKIPPED" and "targetCompleted" in identity:
                    if not mapping(identity["targetCompleted"]).get("aspect"):
                        skipped_owners.add(Owner.parse(identity["targetCompleted"]))
                announced.update(Attempt.parse(child["testResult"]) for child in children if "testResult" in child)
                if "completed" in event:
                    completion = mapping(identity.get("targetCompleted"))
                    success = mapping(event["completed"]).get("success") is True
                    if completion.get("aspect"):
                        aspect = Aspect.parse(completion)
                        if aspect in completed_aspects:
                            raise ValueError(f"Duplicate aspect completion: {aspect}")
                        completed_aspects[aspect] = success
                    else:
                        completed[Owner.parse(completion)] = success
                if "testResult" in event:
                    attempt = Attempt.parse(identity.get("testResult"))
                    if attempt in results:
                        raise ValueError(f"Duplicate test result: {attempt}")
                    results[attempt] = mapping(event["testResult"])
                if "testSummary" in event:
                    owner = Owner.parse(identity.get("testSummary"))
                    if owner in summaries:
                        raise ValueError(f"Duplicate test summary: {owner}")
                    summaries[owner] = mapping(event["testSummary"])
                if "finished" in event:
                    if finished:
                        raise ValueError("Multiple invocation finish events")
                    finished = mapping(event["finished"])
            except (ValueError, TypeError) as error:
                errors.append(f"BEP line {line_number}: {error}")
    configured -= skipped_owners
    labels = set(unit_matrix.owner_labels(str(expected_targets), allow_empty=True)) if expected_targets else set()
    expected: set[Owner] = set()
    expected_aspects: set[Aspect] = set()
    negative_tests: set[Owner] = set()
    marshal_tests: set[Owner] = set()
    declared_benchmark_tests: set[Owner] = set()
    build_groups: set[tuple[Owner, str]] = set()
    for analysis in expected_analysis:
        try:
            targets = unit_matrix.configured_targets(
                str(analysis), tests=True if info["bazel_command"] == "test" else None,
                allow_test_no_build=info["bazel_command"] == "test",
            )
            expected.update(Owner(label, target.configuration) for label, target in targets.items())
            if info["command_kind"] != "analysis":
                build_groups.update(declared_output_groups(targets))
            negative_tests.update(Owner(label, target.configuration) for label, target in targets.items()
                                  if "expected-failure" in target.tags)
            marshal_tests.update(Owner(label, target.configuration) for label, target in targets.items()
                                 if "go-marshal-test-contract" in target.tags)
            jtreg_tests.update(Owner(label, target.configuration) for label, target in targets.items()
                               if "jtreg-results" in target.tags)
            nodejs_tests.update(Owner(label, target.configuration) for label, target in targets.items()
                                if "nodejs-results" in target.tags)
            if info["command_kind"] == "test":
                declared_benchmark_tests.update(Owner(label, target.configuration) for label, target in targets.items()
                                                if "go-benchmark-contract" in target.tags)
            expected_aspects.update(Aspect(Owner(label, target.configuration), name)
                                    for label, target in targets.items() for name in target.aspects)
        except (OSError, ValueError) as error:
            errors.append(f"Expected analysis: {error}")
    build_outputs = []
    try:
        advertised = advertised_build_outputs(events, build_groups)
        captured = sequence(retained.get("build_outputs", []))
        if len(advertised) != len(captured):
            raise ValueError("Retained build outputs differ from the declared output groups")
        for item, raw in zip(advertised, captured, strict=True):
            row = mapping(raw)
            if item != {key: value for key, value in row.items() if key != "path"}:
                raise ValueError(f"Retained build output differs from BEP: {item['name']}")
            path = (manifest.parent / string(row.get("path"))).resolve()
            path.relative_to(manifest.parent.resolve())
            if file_record(path) != {"sha256": item["sha256"], "bytes": item["bytes"]}:
                raise ValueError(f"Retained build output digest/size mismatch: {item['name']}")
            build_outputs.append(row)
    except (OSError, ValueError) as error:
        errors.append(str(error))
    if info["command_kind"] in ("test", "benchmark", "build") and not expected_analysis and not labels:
        errors.append("No independent expected selection was retained")
    if not expected_analysis:
        expected_aspects = set(aspects)
    if labels and expected and labels != {owner.label for owner in expected}:
        errors.append("Expected labels differ from selected analysis leaves")
    if not expected:
        expected = {owner for owner in configured if not labels or owner.label in labels}
    if labels - {owner.label for owner in expected}:
        errors.append("Expected labels absent from configured invocation")
    if not expected:
        errors.append("No expected configured owners")
    # A test command may also build test rules excluded by its execution
    # filters. Independent test-only analysis owns selection; results and
    # summaries below must still belong to that exact set of configurations.
    different = expected - configured if testing else configured ^ expected
    if different:
        errors.append(f"Configured owners differ from expected: {sorted(different)}")
    if benchmark_mode and set(benchmark_expected) != {owner.label for owner in expected}:
        errors.append("Benchmark manifest must cover every selected owner, and no others")
    selected_declared = set(declared_expected) & {owner.label for owner in expected}
    if selected_declared != {owner.label for owner in declared_benchmark_tests}:
        errors.append("Declared benchmark expectations differ from tagged configured owners")
    if declared_benchmarks is not None:
        declared_evidence["owners"] = {label: [asdict(leaf) for leaf in declared_expected[label]]
                                      for label in sorted(selected_declared)}
    if not finished:
        errors.append("Missing invocation finish event")
    accepted_analysis_exit = False
    if info["command_kind"] == "analysis":
        selection = preflight(events, number(info["native_exit"]), string(info["bazel_command"]))
        errors.extend(string(error) for error in sequence(selection["errors"]))
        if selection["status"] == "PASS":
            accepted_analysis_exit = True
            # Analysis completes configured roots through NO_BUILD events,
            # not successful target executions.
            completed.update((owner, True) for owner in expected)
    if info["native_exit"] == 0 and finished.get("overallSuccess") is not True and not accepted_analysis_exit:
        errors.append("Native success disagrees with BEP finish")
    missing = [asdict(owner) for owner in sorted(expected - completed.keys())]
    missing.extend({"label": label, "configuration": None, "reason": "expected owner not configured"}
                   for label in sorted(labels - {owner.label for owner in expected}))
    failures: list[dict[str, object]] = [{**asdict(owner), "reason": "target build failed"} for owner, ok in sorted(completed.items()) if not ok]
    if info["command_kind"] != "analysis":
        aspects = {aspect for aspect in aspects if aspect.owner not in skipped_owners}
        if expected_analysis:
            different_aspects = expected_aspects - aspects if testing else aspects ^ expected_aspects
            if different_aspects:
                errors.append(f"Configured aspects differ from expected: {sorted(different_aspects)}")
        missing.extend({**asdict(aspect.owner), "aspect": aspect.name, "reason": "aspect completion missing"}
                       for aspect in sorted((aspects | expected_aspects) - completed_aspects.keys()))
        if completed_aspects.keys() - aspects:
            errors.append("Unannounced aspect completions")
        failures.extend({**asdict(aspect.owner), "aspect": aspect.name, "reason": "aspect build failed"}
                        for aspect, ok in sorted(completed_aspects.items()) if not ok)
    cases: Counter[str] = Counter()
    observations: list[dict[str, object]] = []
    owner_cases: Counter[Owner] = Counter()
    owner_executed: Counter[Owner] = Counter()
    skips: list[dict[str, object]] = []
    expected_failures: list[dict[str, object]] = []
    marshal_contracts: dict[Attempt, dict[str, object]] = {}
    non_runtime_owners: list[dict[str, object]] = []
    if testing:
        missing.extend({**asdict(attempt), "reason": "announced result missing"} for attempt in sorted(announced - results.keys()))
        if results.keys() - announced:
            errors.append("Unannounced test results")
        missing.extend({**asdict(owner), "reason": "test summary missing"} for owner in sorted(expected - summaries.keys()))
        result_owners = {attempt.owner for attempt in results}
        missing.extend({**asdict(owner), "reason": "test result missing"} for owner in sorted(expected - result_owners))
        if result_owners - expected or summaries.keys() - expected:
            errors.append("Results or summaries belong to an unexpected configuration")
        for attempt, result in sorted(results.items()):
            observation: dict[str, object] = {**asdict(attempt), "status": result.get("status"),
                                            "cached_locally": result.get("cachedLocally", False),
                                            "execution_info": result.get("executionInfo", {})}
            for contract, owners, parser in (("jtreg", jtreg_tests, jtreg_results),
                                             ("nodejs", nodejs_tests, nodejs_results)):
                if attempt.owner not in owners:
                    continue
                try:
                    runtime = parser(reports[(attempt, "test.log")], attempt, reports)
                    observation[contract] = runtime
                    errors.extend(f"{attempt}: {error}" for error in runtime["errors"])
                    failures.extend({**asdict(attempt), "reason": f"{contract} failure", "name": name}
                                    for name in runtime["failures"])
                    skips.extend({**asdict(attempt), "reason": reason, "name": name}
                                 for name, reason in runtime["skipped"].items())
                    missing.extend({**asdict(attempt), "reason": f"{contract} result missing", "name": name}
                                   for name in runtime["missing"])
                    if contract == "nodejs" and not runtime["passed"] and not runtime["failures"]:
                        missing.append({**asdict(attempt), "reason": "No Node expected-outcome successes"})
                except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
                    errors.append(f"{attempt}: {error}")
            try:
                xml = reports[(attempt, "test.xml")]
                log = reports[(attempt, "test.log")]
                if not log.stat().st_size:
                    raise ValueError(f"Empty original test log: {attempt}")
                counts = xml_cases(xml)
                observation["xml"] = counts
                cases.update({key: number(counts[key]) for key in ("passed", "failed", "skipped")})
                owner_cases[attempt.owner] += sum(number(counts[key]) for key in ("passed", "failed", "skipped"))
                owner_executed[attempt.owner] += number(counts["passed"]) + number(counts["failed"])
                skips.extend({**asdict(attempt), **mapping(item)} for item in sequence(counts["skips"]))
                if counts["failed"]:
                    failure = {**asdict(attempt), "reason": "XML test failures", "cases": counts["failures"]}
                    # A negative-test wrapper can pass precisely because its
                    # child failed. Retain those failures as unsupported cases,
                    # without treating them as successful child coverage.
                    if (attempt.owner in negative_tests and result.get("status") == "PASSED"
                            and summaries.get(attempt.owner, {}).get("overallStatus") == "PASSED"
                            and all(string(name).strip() for name in sequence(counts["failures"]))):
                        expected_failures.append(failure)
                    else:
                        failures.append(failure)
                elif attempt.owner in negative_tests:
                    errors.append(f"Expected-failure owner has no failing named cases: {attempt}")
                if attempt.owner in marshal_tests:
                    contract = marshal_test_contract(log)
                    marshal_contracts[attempt] = contract
                    observation["marshal_test_contract"] = contract
                    if (not contract["runtime_types"] or not contract["constraints_match"]) and owner_cases[attempt.owner]:
                        errors.append(f"Generated ABI test declares no applicable runtime tests but has named cases: {attempt}")
                declared_benchmark = attempt.owner in declared_benchmark_tests
                if benchmark_mode or declared_benchmark:
                    rows = benchmark_rows(log, pgo=pgo_expected is not None)
                    observation["benchmarks"] = rows
                    expectations = declared_expected if declared_benchmark else benchmark_expected
                    wanted = {leaf.name: leaf for leaf in expectations.get(attempt.owner.label, [])}
                    # Go appends -GOMAXPROCS to benchmark names. Retain the
                    # observed suffix, but match default-CPU runs by leaf name.
                    # An exact leaf may itself end in digits. Explicit -test.cpu
                    # contracts still require exact names.
                    # https://pkg.go.dev/golang.org/x/perf/benchfmt#Name.Parts
                    names = [re.sub(r"-\d+$", "", string(row["name"]))
                             if (declared_benchmark or benchmark_default_cpu and pgo_expected is None)
                             and string(row["name"]) not in wanted else string(row["name"])
                             for row in rows]
                    actual = Counter(names)
                    if actual != Counter({name: leaf.count for name, leaf in wanted.items()}):
                        errors.append(f"Benchmark rows differ for {attempt}: got {dict(actual)}, expected {wanted}")
                    for name, row in zip(names, rows):
                        if (leaf := wanted.get(name)) is not None:
                            units = set(mapping(row["metrics"]))
                            units_match = set(leaf.units) <= units if pgo_expected is not None else set(leaf.units) == units
                            if not units_match or leaf.iterations is not None and row["iterations"] != leaf.iterations:
                                errors.append(f"Benchmark metrics/iterations differ for {attempt}: {row}")
                    if pgo_expected is not None:
                        profile = pgo_expected[attempt.owner.label]
                        excluded = profile.excluded
                        benchmark_cases = list(ET.parse(xml).getroot().iter("testcase"))
                        observed_skips = [case.get("name", "") for case in benchmark_cases
                                          if case.find("skipped") is not None]
                        observation["pgo_excluded"] = observed_skips
                        observation["pgo"] = pgo_result(attempt, reports, profile)
                        if Counter(observed_skips) != Counter(excluded):
                            errors.append(f"PGO exclusions differ for {attempt}: got {observed_skips}, expected {excluded}")
                        # Verbose Go wrappers may emit benchmark testcase XML.
                        # Coverage still requires every active measurement row.
                        for case in benchmark_cases:
                            if not case.get("name", "").startswith("Benchmark"):
                                errors.append(f"PGO invocation contains a named test: {attempt}: {case.get('name')}")
                    elif counts["passed"] or counts["skipped"]:
                        errors.append(f"Benchmark-only invocation contains named tests: {attempt}")
            except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile) as error:
                errors.append(f"{attempt}: {error}")
            if result.get("status") != "PASSED":
                failures.append({**asdict(attempt), "reason": "test result", "status": result.get("status")})
            observations.append(observation)
        for owner, summary in sorted(summaries.items()):
            attempts = [attempt for attempt in results if attempt.owner == owner]
            if number(summary.get("totalRunCount", 0)) != len(attempts):
                errors.append(f"Summary result count differs for {owner}")
            runs = number(summary.get("runCount", 1))
            shards = number(summary.get("shardCount", 1))
            if runs < 1 or shards < 1:
                errors.append(f"Invalid summary run/shard dimensions for {owner}")
            elif runs * shards > len(attempts):
                errors.append(f"Summary run/shard coverage is incomplete for {owner}")
            elif {(attempt.run, attempt.shard) for attempt in attempts} != {
                (run, shard) for run in range(1, runs + 1) for shard in range(1, shards + 1)
            }:
                errors.append(f"Summary run/shard identities differ for {owner}")
            if summary.get("overallStatus") != "PASSED":
                failures.append({**asdict(owner), "reason": "test summary", "status": summary.get("overallStatus")})
        for owner in sorted(expected | result_owners):
            attempts = [attempt for attempt in results if attempt.owner == owner]
            contracts = [marshal_contracts[attempt] for attempt in attempts if attempt in marshal_contracts]
            consistent = bool(contracts) and len(contracts) == len(attempts) and all(contract == contracts[0] for contract in contracts)
            if contracts and not consistent:
                errors.append(f"Generated ABI test applicability differs or is missing across attempts: {owner}")
            non_runtime = (consistent and owner_cases[owner] == 0
                           and (not contracts[0]["runtime_types"] or not contracts[0]["constraints_match"])
                           and all(results[attempt].get("status") == "PASSED" for attempt in attempts)
                           and summaries.get(owner, {}).get("overallStatus") == "PASSED")
            if non_runtime:
                non_runtime_owners.append({**asdict(owner), **contracts[0]})
            if (not benchmark_mode and owner not in declared_benchmark_tests
                    and not non_runtime and owner_executed[owner] == 0):
                errors.append(f"Test owner has no executed named cases; zero-case/all-skipped success is not coverage: {owner}")
    elif results or summaries:
        errors.append("Build/analysis invocation unexpectedly contains test execution")
    if info["capture_exit"] != 0:
        errors.append("Caller recorded a capture failure")
    status = "FAIL" if (info["native_exit"] != 0 and not accepted_analysis_exit) or failures else "INCOMPLETE" if errors or missing else "PASS"
    now = datetime.datetime.now(datetime.timezone.utc)
    return {"schema_version": 1, **info, "status": status, "accepted_analysis_exit": accepted_analysis_exit,
            "errors": errors, "failures": failures,
            "build_outputs": build_outputs,
            "expected_labels": sorted(labels or {owner.label for owner in expected}),
            "expected": [asdict(owner) for owner in sorted(expected)], "executed": observations,
            "expected_aspects": [{**asdict(aspect.owner), "aspect": aspect.name} for aspect in sorted(expected_aspects)],
            "completed_aspects": [{**asdict(aspect.owner), "aspect": aspect.name, "success": ok}
                                  for aspect, ok in sorted(completed_aspects.items())],
            "missing": missing, "skips": skips, "case_counts": dict(cases),
            "expected_failures": expected_failures,
            "non_runtime_owners": non_runtime_owners,
            "benchmark_manifest": benchmark_evidence,
            "declared_benchmark_manifest": declared_evidence,
            "zero_case_owners": [asdict(owner) for owner, count in sorted(owner_cases.items()) if count == 0],
            "all_skipped_owners": [asdict(owner) for owner, count in sorted(owner_executed.items()) if count == 0 and owner_cases[owner] > 0],
            "skipped_owners": [asdict(owner) for owner in sorted(skipped_owners)],
            "bep_finish": finished, "verdict_available_at": now.isoformat(),
            "reporting_elapsed_seconds": time.monotonic() - started,
            "execution_to_verdict_seconds": (now - timestamp(string(info["execution_finished_at"]))).total_seconds()
            if info["execution_finished_at"] is not None else None}


def bundle(directory: Path, runner_exit: int, capture_exit: int, output: Path) -> dict[str, object]:
    """Keep original command exits distinct from the enclosing runner's exit."""
    rows = []
    errors = []
    for invocation in sorted(directory.rglob("invocation-*")):
        if not invocation.is_dir():
            continue
        path = invocation / "verdict.json"
        try:
            rows.append({"path": str(path.relative_to(directory)), "verdict": read_json(path), **file_record(path)})
        except (OSError, ValueError) as error:
            errors.append(str(error))
    if not rows:
        errors.append("No invocation verdicts were retained")
    sources = {string(mapping(row["verdict"]).get("source")) for row in rows}
    if len(sources) != 1:
        errors.append("Invocation sources are absent or inconsistent")
    verdicts = [mapping(row["verdict"]) for row in rows]
    native_exits = []
    for row in rows:
        try:
            value = mapping(row["verdict"])
            native_exits.append({"invocation": row["path"], "exit": number(value.get("native_exit")),
                                 "accepted_analysis_exit": value.get("accepted_analysis_exit") is True})
        except ValueError as error:
            errors.append(str(error))
    native_exit = next((number(row["exit"]) for row in native_exits if row["exit"] != 0 and not row["accepted_analysis_exit"]), 0) if native_exits else None
    coordinator_failed = False
    if native_exit or any(row.get("status") == "FAIL" for row in verdicts):
        status = "FAIL"
    elif capture_exit or errors or any(row.get("status") != "PASS" for row in verdicts):
        status = "INCOMPLETE"
    elif runner_exit:
        status = "FAIL"
        coordinator_failed = True
    else:
        status = "PASS"
    result = {"schema_version": 1, "status": status, "sources": sorted(sources), "native_exit": native_exit,
              "native_exits": native_exits, "runner_exit": runner_exit, "coordinator_failed": coordinator_failed,
              "capture_exit": capture_exit, "invocations": rows, "errors": errors,
              "expected": [{"invocation": row["path"], **mapping(item)} for row in rows for item in sequence(mapping(row["verdict"]).get("expected", []))],
              "executed": [{"invocation": row["path"], **mapping(item)} for row in rows for item in sequence(mapping(row["verdict"]).get("executed", []))],
              "commands": [{"invocation": row["path"], **{key: mapping(row["verdict"])[key] for key in (
                  "source", "status", "bazel_command", "arguments", "native_exit", "command_outputs", "execution_finished_at",
              )}} for row in rows if mapping(row["verdict"]).get("command_kind") == "command"],
              "case_counts": dict(sum((Counter({key: number(value) for key, value in mapping(row.get("case_counts", {})).items()}) for row in verdicts), Counter())),
              "zero_case_owners": [item for row in verdicts for item in sequence(row.get("zero_case_owners", []))],
              "non_runtime_owners": [item for row in verdicts for item in sequence(row.get("non_runtime_owners", []))],
              "all_skipped_owners": [item for row in verdicts for item in sequence(row.get("all_skipped_owners", []))],
              "failures": [item for row in verdicts for item in sequence(row.get("failures", []))],
              "build_outputs": [{"invocation": row["path"], **mapping(item)} for row in rows
                                for item in sequence(mapping(row["verdict"]).get("build_outputs", []))],
              "expected_failures": [item for row in verdicts for item in sequence(row.get("expected_failures", []))],
              "missing": [item for row in verdicts for item in sequence(row.get("missing", []))],
              "skips": [item for row in verdicts for item in sequence(row.get("skips", []))],
              "verdict_available_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    write_json(directory / "verdict.json", result)
    # The archive must be outside its input directory, and symlinks cannot
    # introduce private files from outside the owned artifact directory.
    try:
        output.resolve().relative_to(directory.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("Bundle output must be outside the evidence directory")
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Symlink in evidence directory: {path}")
            if path.is_file():
                archive.add(path, arcname=str(path.relative_to(directory)), recursive=False)
    return result


def receipt(artifact: Path, sha256: str, expected_source: str,
            targets: list[str] | None = None, *, deadline: float | None = None) -> dict[str, object]:
    """Authenticate a downloaded artifact and retain its producer verdict."""
    identity = file_record(artifact, deadline=deadline)
    if identity["sha256"] != sha256.removeprefix("sha256:"):
        raise ValueError("Artifact SHA256 differs from the expected digest")
    # Reconciliation already happened on the coordinator. Read only its
    # verdict, from either the Actions upload or the native Remote Bazel bundle.
    def read_producer(stream: BinaryIO) -> dict[str, object]:
        reader = DeadlineReader(stream, deadline)
        value = mapping(json.loads(b"".join(iter(lambda: reader.read(1024 * 1024), b""))))
        check_deadline(deadline)
        return value

    if zipfile.is_zipfile(artifact):
        with zipfile.ZipFile(artifact) as archive:
            member = archive.getinfo("qualification/verdict.json")
            if member.file_size > MAX_REPORT_BYTES:
                raise ValueError("Producer verdict exceeds the report size bound")
            with archive.open(member) as stream:
                producer = read_producer(stream)
    else:
        with artifact.open("rb") as source, tarfile.open(fileobj=DeadlineReader(source, deadline), mode="r|gz") as archive:
            for member in archive:
                if member.name != "verdict.json":
                    continue
                if not member.isfile():
                    raise ValueError("Producer verdict is not a regular file")
                if member.size > MAX_REPORT_BYTES:
                    raise ValueError("Producer verdict exceeds the report size bound")
                with archive.extractfile(member) as stream:
                    producer = read_producer(stream)
                break
            else:
                raise ValueError("Producer verdict is missing from the bundle")
    if producer.get("schema_version") != 1 or producer.get("status") not in ("PASS", "FAIL", "INCOMPLETE"):
        raise ValueError("Unsupported producer verdict")
    if producer.get("sources") != [expected_source]:
        raise ValueError(f"Producer sources {producer.get('sources')} differ from {expected_source}")
    expected = sequence(producer["expected"])
    labels = sorted({string(mapping(owner)["label"]) for owner in expected})
    if targets is not None and set(targets) != set(labels):
        raise ValueError(f"Producer target labels differ: expected {sorted(set(targets))}, got {labels}")
    # Keep outcome and coverage fields verbatim. Summarize only the large
    # attempt records; callers can inspect those in the authenticated bundle.
    result = {key: value for key, value in producer.items() if key not in ("expected", "executed", "invocations")}
    invocations = []
    for raw in sequence(producer["invocations"]):
        row = mapping(raw)
        verdict = mapping(row["verdict"])
        invocation = {"path": row["path"], **{key: verdict[key] for key in (
            "source", "status", "command_kind", "native_exit", "capture_exit", "accepted_analysis_exit",
            "errors", "execution_finished_at", "bep_finish", "verdict_available_at",
        )}}
        for key in ("expected_aspects", "completed_aspects"):
            if key in verdict:
                invocation[key] = verdict[key]
        invocations.append(invocation)
    check_deadline(deadline)
    return {**result, "artifact": {"path": str(artifact), **identity},
            "expected_labels": labels, "expected_owner_count": len(expected),
            "executed_attempt_count": len(sequence(producer["executed"])), "invocations": invocations,
            "consumer_verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def native_receipt(invocation_id: str, api_key_file: Path, artifact: Path, output: Path,
                   expected_source: str, targets: list[str] | None, timeout_seconds: float) -> dict[str, object]:
    """Wait for one native invocation and authenticate its qualification bundle."""
    partial = artifact.with_name(artifact.name + ".partial")
    if output.resolve() in (artifact.resolve(), partial.resolve()):
        raise ValueError("Artifact, partial download and receipt paths must differ")
    execution: dict[str, object] = {"invocationId": invocation_id}
    observation_retries: list[dict[str, str]] = []
    result: dict[str, object]
    deadline = time.monotonic() + timeout_seconds

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Native receipt collection exceeded its deadline; attach again to resume")
        return seconds

    @contextmanager
    def request(method: str, body: dict[str, object]) -> Iterator[HTTPResponse]:
        # A fixed origin and direct HTTPS connection never forward the key on
        # redirects. Response bodies and credentials are not included in errors.
        connection = HTTPSConnection("app.buildbuddy.io", timeout=min(30, remaining()))
        try:
            connection.request("POST", "/api/v1/" + method, json.dumps(body),
                               {"Content-Type": "application/json", "x-buildbuddy-api-key": api_key})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise ValueError(f"BuildBuddy {method} returned HTTP {response.status}")
                yield response
        finally:
            connection.close()

    try:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Collection timeout must be positive and finite")
        if str(uuid.UUID(invocation_id)) != invocation_id:
            raise ValueError("Expected a canonical invocation UUID")
        if re.fullmatch(r"[0-9a-f]{40}", expected_source) is None:
            raise ValueError("Expected a full source commit SHA")
        api_key = api_key_file.read_text().strip()
        if not api_key or any(not 33 <= ord(character) <= 126 for character in api_key):
            raise ValueError("BuildBuddy API key file must contain one nonempty ASCII token")
        while True:
            try:
                with request("GetInvocation", {"selector": {"invocation_id": invocation_id},
                                               "include_artifacts": True}) as response:
                    raw = bytearray()
                    while True:
                        remaining()
                        chunk = response.read1(64 * 1024)
                        remaining()
                        if not chunk:
                            break
                        raw.extend(chunk)
                        if len(raw) > 4 * 1024 * 1024:
                            raise ValueError("BuildBuddy invocation metadata exceeds 4 MiB")
            except (TimeoutError, ConnectionResetError) as error:
                # A failed status read says nothing about the running build.
                # Retry that read within the original collection deadline.
                remaining()
                observation_retries.append({
                    "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "error": type(error).__name__,
                })
                time.sleep(min(5, remaining()))
                continue
            match sequence(mapping(json.loads(raw)).get("invocation", [])):
                case []:
                    invocation = {}
                case [dict() as invocation]:
                    if mapping(invocation.get("id")).get("invocationId") != invocation_id:
                        raise ValueError("BuildBuddy returned a different invocation")
                    execution = {"invocationId": invocation_id, **{key: invocation[key] for key in (
                        "invocationStatus", "success", "bazelExitCode", "commitSha", "createdAtUsec",
                        "updatedAtUsec", "durationUsec",
                    ) if key in invocation}}
                    if invocation.get("commitSha") and invocation["commitSha"] != expected_source:
                        raise ValueError("BuildBuddy invocation source differs from the submitted source")
                case _:
                    raise ValueError("BuildBuddy returned multiple or malformed invocations")
            if invocation.get("invocationStatus") in ("COMPLETE_INVOCATION_STATUS", "DISCONNECTED_INVOCATION_STATUS"):
                # Proto JSON may omit false; other JSON types are not booleans.
                if not isinstance(invocation.get("success", False), bool):
                    raise ValueError("Terminal invocation success is not a boolean")
                break
            time.sleep(min(5, remaining()))
        execution["terminal_observed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if invocation.get("commitSha") != expected_source:
            raise ValueError("Terminal invocation is missing the submitted source")
        matches = [mapping(item) for item in sequence(invocation.get("artifacts", []))
                   if mapping(item).get("name") == "qualification.tar.gz"]
        match matches:
            case [bundle_record]:
                uri = string(bundle_record.get("uri"))
            case _:
                raise ValueError(f"Expected exactly one qualification.tar.gz artifact, got {len(matches)}")
        parsed = urlsplit(uri)
        identity = re.search(r"/blobs/([0-9a-f]{64})/([0-9]+)$", parsed.path)
        if parsed.scheme != "bytestream" or not parsed.netloc or identity is None:
            raise ValueError("Qualification artifact is missing its SHA256 CAS identity")
        sha256, size_text = identity.groups()
        size = int(size_text)
        expected_identity = {"sha256": sha256, "bytes": size}
        execution["artifact"] = {"uri": uri, **expected_identity}
        if not artifact.exists():
            # GetFile returns raw bytes over HTTP. Interrupted transfers are
            # restarted, never appended without provider-supported ranges.
            with request("GetFile", {"uri": uri}) as response, partial.open("wb") as stream:
                downloaded = 0
                digest = hashlib.sha256()
                while True:
                    remaining()
                    chunk = response.read1(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > size:
                        raise ValueError("Downloaded artifact exceeds its advertised size")
                    digest.update(chunk)
                    stream.write(chunk)
            if {"sha256": digest.hexdigest(), "bytes": downloaded} != expected_identity:
                raise ValueError("Downloaded artifact differs from its advertised SHA256 or size")
            partial.replace(artifact)
        if artifact.stat().st_size != size:
            raise ValueError("Existing artifact differs from its advertised size")
        remaining()
        result = receipt(artifact, sha256, expected_source, targets, deadline=deadline)
        result["producer_status"] = result["status"]
        if execution.get("invocationStatus") == "COMPLETE_INVOCATION_STATUS" and execution.get("success", False) is False:
            result["status"] = "FAIL"
        elif execution.get("invocationStatus") != "COMPLETE_INVOCATION_STATUS" and result["status"] != "FAIL":
            result["status"] = "INCOMPLETE"
        remaining()
        result["collection_status"] = "COMPLETE"
    except (OSError, HTTPException, ValueError, KeyError, EOFError, zipfile.BadZipFile, tarfile.TarError) as error:
        failed = execution.get("invocationStatus") == "COMPLETE_INVOCATION_STATUS" and execution.get("success", False) is False
        result = {"schema_version": 1, "status": "FAIL" if failed else "INCOMPLETE",
                  "collection_status": "ERROR", "collection_error": str(error)}
    result["native_invocation"] = execution
    result["observation_retries"] = observation_retries
    result["submitted_source"] = expected_source
    result["collection_finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    write_json(output, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    copy = commands.add_parser("capture")
    for name in ("events", "output", "output-root", "report-dir"):
        copy.add_argument("--" + name, type=Path, required=True)
    copy.add_argument("--retain-outputs", action="store_true")
    copy.add_argument("--expected-analysis", type=Path, action="append", default=[])
    analyze = commands.add_parser("preflight")
    for name in ("events", "output"):
        analyze.add_argument("--" + name, type=Path, required=True)
    analyze.add_argument("--native-exit", type=int, required=True)
    analyze.add_argument("--kind", choices=("test", "build"), required=True)
    analyze.add_argument("--output-options", type=Path)
    check = commands.add_parser("verdict")
    for name in ("events", "context", "output"):
        check.add_argument("--" + name, type=Path, required=True)
    check.add_argument("--manifest", type=Path)
    check.add_argument("--expected-targets", type=Path)
    check.add_argument("--expected-analysis", type=Path, action="append", default=[])
    check.add_argument("--expected-benchmarks", type=Path)
    check.add_argument("--declared-benchmarks", type=Path)
    command_check = commands.add_parser("command-verdict")
    for name in ("context", "output"):
        command_check.add_argument("--" + name, type=Path, required=True)
    benchmarks = commands.add_parser("benchmark-arguments")
    benchmarks.add_argument("--manifest", type=Path, required=True)
    benchmarks.add_argument("--output", type=Path, required=True)
    benchmarks.add_argument("--target", action="append", required=True)
    benchmarks.add_argument("--selected-manifest", type=Path)
    pack = commands.add_parser("bundle")
    for name in ("directory", "output"):
        pack.add_argument("--" + name, type=Path, required=True)
    for name in ("runner-exit", "capture-exit"):
        pack.add_argument("--" + name, type=int, required=True)
    consume = commands.add_parser("receipt")
    for name in ("artifact", "output"):
        consume.add_argument("--" + name, type=Path, required=True)
    for name in ("sha256", "expected-source"):
        consume.add_argument("--" + name, required=True)
    consume.add_argument("--target", action="append")
    native = commands.add_parser("native-receipt")
    for name in ("api-key-file", "artifact", "output"):
        native.add_argument("--" + name, type=Path, required=True)
    for name in ("invocation-id", "expected-source"):
        native.add_argument("--" + name, required=True)
    native.add_argument("--target", action="append")
    native.add_argument("--timeout-seconds", type=float, required=True)
    args = parser.parse_args()
    if args.command == "native-receipt":
        try:
            result = native_receipt(args.invocation_id, args.api_key_file, args.artifact, args.output,
                                    args.expected_source, args.target, args.timeout_seconds)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        print(f"{result['status']}: {args.output}")
        return int(result["status"] != "PASS")
    if args.command == "receipt":
        try:
            result = receipt(args.artifact, args.sha256, args.expected_source, args.target)
        except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile, tarfile.TarError) as error:
            parser.error(str(error))
        write_json(args.output, result)
        print(f"{result['status']}: {args.output}")
        return int(result["status"] != "PASS")
    if args.command == "benchmark-arguments":
        arguments, owners = benchmark_manifest(args.manifest)
        raw = read_json(args.manifest)
        if args.selected_manifest is not None:
            if "pgo" not in raw or len(set(args.target)) != len(args.target) or not set(args.target) <= set(owners):
                parser.error("PGO selection must contain unique declared owners")
            raw["owners"] = {owner: mapping(raw["owners"])[owner] for owner in args.target}
            raw["pgo"] = {owner: mapping(raw["pgo"])[owner] for owner in args.target}
            write_json(args.selected_manifest, raw)
        elif set(args.target) != set(owners):
            parser.error("Benchmark manifest owners differ from selected targets")
        args.output.write_text("".join(argument + "\n" for argument in arguments))
        return 0
    if args.command == "capture":
        result = capture(args.events, args.output, args.output_root, args.report_dir, args.retain_outputs,
                         tuple(args.expected_analysis))
        return int(bool(result["errors"]))
    if args.command == "preflight":
        result = preflight(args.events, args.native_exit, args.kind)
        write_json(args.output, result)
        if args.output_options is not None and result["status"] == "PASS":
            args.output_options.write_text("".join(option + "\n" for option in result["output_options"]))
        return int(result["status"] != "PASS")
    if args.command == "verdict":
        result = verdict(args.events, args.manifest or manifest_path(args.events), args.context,
                         args.expected_targets, tuple(args.expected_analysis), args.expected_benchmarks,
                         args.declared_benchmarks)
        write_json(args.output, result)
        return 0 if result["status"] == "PASS" else number(result["native_exit"]) or 1
    if args.command == "command-verdict":
        result = command_verdict(args.context)
        write_json(args.output, result)
        return 0 if result["status"] == "PASS" else result["native_exit"] or 1
    result = bundle(args.directory, args.runner_exit, args.capture_exit, args.output)
    exit_code = number(result["native_exit"]) if result["native_exit"] else args.runner_exit or int(result["status"] != "PASS")
    # The coordinator can upload the archive and then lose its build-event
    # stream. Retain its CAS identity in the execution log for recovery.
    try:
        archive_identity = (json.dumps({"qualification_archive": file_record(args.output),
                                       "sources": result["sources"], "status": result["status"]}) + "\n").encode()
        # A buffered write to a closed pipe can override the original exit
        # during Python's shutdown flush, even after catching BrokenPipeError.
        if os.write(1, archive_identity) != len(archive_identity):
            raise OSError("Incomplete archive identity write")
    except OSError:
        if not exit_code:
            raise
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
