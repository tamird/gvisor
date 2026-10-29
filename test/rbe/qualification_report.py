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
from collections import Counter, deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import datetime
import errno
import hashlib
from http.client import HTTPException, HTTPResponse, HTTPSConnection
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
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
NODE_RESULT_TAGS = {"nodejs-results", "nodejs-exact-results"}
RUNTIME_RESULT_TAGS = {"jtreg-results", "php-results"} | NODE_RESULT_TAGS
PHP_OUTPUT_NAME = r"php-results\.[A-Za-z0-9]+/(?:selection\.txt|root\.txt|redirect\.txt|results\.tsv|junit\.xml)"
RESULT_OUTPUT_PATTERNS = {
    **dict.fromkeys(RUNTIME_RESULT_TAGS, RUNTIME_OUTPUT_NAME),
    "php-results": "(?:" + RUNTIME_OUTPUT_NAME + "|" + PHP_OUTPUT_NAME + ")",
    "source-image-results": (r"source-images/(?:selection\.json|[A-Za-z0-9][A-Za-z0-9_.-]*/"
                             r"(?:request\.json|build-exit-code|inspect\.json|exit-code))"),
}
SOURCE_IMAGE_ASPECT = "//test/docker:source_image_contracts.bzl%source_image_contracts"
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


def retained_reports(manifest: Path) -> tuple[dict[str, object], dict[tuple[Attempt, str], Path], list[str]]:
    """Read the attempt identities and immutable originals owned by capture."""
    retained = read_json(manifest)
    errors = [string(item) for item in sequence(retained.get("errors"))]
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
    return retained, reports, errors


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
        if SOURCE_IMAGE_ASPECT in target.aspects:
            groups.add((Owner(label, target.configuration), "source_image_contracts"))
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
            if "completed" not in event:
                continue
            aspect = mapping(identity.get("targetCompleted")).get("aspect")
            if aspect and aspect != SOURCE_IMAGE_ASPECT:
                continue
            owner = Owner.parse(identity["targetCompleted"])
            for raw in sequence(mapping(event["completed"]).get("outputGroup", [])):
                group = mapping(raw)
                key = owner, string(group.get("name"))
                if key not in groups:
                    continue
                if (key[1] == "source_image_contracts") != (aspect == SOURCE_IMAGE_ASPECT):
                    raise ValueError(f"Build output group has the wrong aspect: {key}")
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


def result_output_pattern(tags: Iterable[str]) -> str:
    """Select the small result files needed even when their tests pass."""
    return "|".join(dict.fromkeys(RESULT_OUTPUT_PATTERNS[tag] for tag in tags if tag in RESULT_OUTPUT_PATTERNS))


def capture(events: Path, output: Path, output_root: Path, report_dir: Path, retain_outputs: bool = False,
            expected_analysis: tuple[Path, ...] = (), expected_kind: str = "build") -> dict[str, object]:
    """Copy all advertised original reports before returning capture failures."""
    errors: list[str] = []
    reports: list[dict[str, object]] = []
    result_patterns: dict[str, str] = {}
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
                    pattern = result_output_pattern(mapping(event.get("configured", {})).get("tag", []))
                    if pattern:
                        identity = mapping(event.get("id"))
                        result_patterns[string(mapping(identity.get("targetConfigured")).get("label"))] = pattern
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
                        elif pattern := result_patterns.get(attempt.owner.label):
                            names.extend(name for name in advertised
                                         if name == "test.outputs__outputs.zip" or re.fullmatch("test.outputs/(?:" + pattern + ")", name))
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
            groups.update(declared_output_groups(unit_matrix.configured_targets(
                str(analysis), tests=True if expected_kind == "test" else None,
                allow_test_no_build=expected_kind == "test")))
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


def xml_cases(path: Path, required: frozenset[str] = frozenset()) -> dict[str, object]:
    """Count actual entries, retaining skips/notrun separately from passes."""
    root = ET.parse(path).getroot()
    if root.tag not in ("testsuites", "testsuite"):
        raise ValueError(f"Unexpected XML root: {root.tag}")
    counts: Counter[str] = Counter()
    failures, skips = [], []
    matched = set()
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
            if name in required:
                matched.add(name)
    # Suite failures can exist without a corresponding named testcase.
    suite_errors = sum(number(suite.get(key, 0)) for suite in root.iter("testsuite") for key in ("failures", "errors"))
    if suite_errors and not counts["failed"]:
        raise ValueError("XML declares failures without failed testcase entries")
    declared = root.get("tests")
    if declared is not None and number(declared) != sum(counts.values()):
        raise ValueError(f"XML declares {declared} tests but contains {sum(counts.values())}")
    return {"passed": counts["passed"], "failed": counts["failed"], "skipped": counts["skipped"],
            "failures": failures, "skips": skips, **({"required_passed": sorted(matched)} if required else {})}


def runtime_outputs(attempt: Attempt, reports: dict[tuple[Attempt, str], Path],
                    pattern: str) -> Iterator[tuple[str, bytes]]:
    """Read selected native artifacts from either BEP output representation."""
    total = 0
    seen: set[str] = set()

    def check(name: str, size: int) -> None:
        nonlocal total
        total += size
        if name in seen:
            raise ValueError(f"Duplicate runtime artifact: {name}")
        seen.add(name)
        if size > MAX_REPORT_BYTES or total > MAX_TOTAL_REPORT_BYTES:
            raise ValueError("Runtime output exceeds retention bounds")

    if archive_path := reports.get((attempt, "test.outputs__outputs.zip")):
        with zipfile.ZipFile(archive_path) as archive:
            for item in archive.infolist():
                if re.fullmatch(pattern, item.filename):
                    check(item.filename, item.file_size)
                    yield item.filename, archive.read(item)
    else:
        for (owner, name), path in reports.items():
            if owner == attempt and name.startswith("test.outputs/"):
                name = name.removeprefix("test.outputs/")
                if re.fullmatch(pattern, name):
                    check(name, path.stat().st_size)
                    yield name, path.read_bytes()


def runtime_batches(log: Path, attempt: Attempt, reports: dict[tuple[Attempt, str], Path]
                    ) -> tuple[list[str], list[tuple[list[str], str]], list[str]]:
    """Bind retained batch output to the runner's original selected names."""
    pattern = r"^I\d{4} \S+\s+\d+ lib\.go:\d+\] Running test case batch: (.+)$"
    batches = [match[1].split(",") for line in log.read_text().splitlines()
               if (match := re.fullmatch(pattern, line))]
    expected = [name for batch in batches for name in batch]
    if not expected or len(expected) != len(set(expected)):
        raise ValueError("Runtime selection is empty or contains duplicate names")

    found_batches = []
    retained = []
    errors = []
    try:
        for _, raw in runtime_outputs(attempt, reports, r"batch-\d+\.log"):
            output = raw.decode()
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


def php_results(log: Path, attempt: Attempt, reports: dict[tuple[Attempt, str], Path]) -> dict[str, object]:
    """Join PHP's native -W states and complete JUnit to each actual command.

    -W retains expected-failure states that JUnit folds into passes, but has no
    completion footer. JUnit is written after the top-level run returns. A
    redirect parent has no -W row; its isolated XML parent and contextual child
    rows establish completion of PHP's enumerated expansion, not discovery of
    every potentially eligible test in the source archive.
    """
    expected, batches, errors = runtime_batches(log, attempt, reports)
    groups: dict[str, dict[str, bytes]] = {}
    for name, content in runtime_outputs(attempt, reports, PHP_OUTPUT_NAME):
        group, name = name.split("/", 1)
        groups.setdefault(group, {})[name] = content
    results: dict[str, dict[str, str]] = {}
    skipped: dict[str, str] = {}
    failures: set[str] = set()
    expected_failures: dict[str, str] = {}
    redirects: dict[str, list[str]] = {}
    completed: set[str] = set()
    counter_only: dict[str, str] = {}
    referenced: list[str] = []
    states = {"PASSED", "SKIPPED", "XFAILED", "XLEAKED", "FAILED", "WARNED", "LEAKED", "BORKED"}

    for selected, body in batches:
        markers = re.findall(r"^gvisor-php-report /proctor-artifacts/(php-results\.[A-Za-z0-9]+)\r?$", body, re.M)
        referenced.extend(markers)
        selections: Counter[str] = Counter()
        for group in markers:
            native: dict[str, str] = {}
            results[group] = native
            try:
                files = groups[group]
                names = files["selection.txt"].decode().splitlines()
                if not names or len(names) != len(set(names)) or not set(names) <= set(selected):
                    raise ValueError("PHP command selection differs from its batch")
                positions = [selected.index(name) for name in names]
                if positions != list(range(positions[0], positions[0] + len(names))):
                    raise ValueError("PHP command selection is not an ordered batch group")
                selections.update(names)
                source_root = files["root.txt"].decode().removesuffix("\n")
                if not source_root.startswith("/") or str(PurePosixPath(source_root)) != source_root:
                    raise ValueError("Invalid PHP source root")
                redirect = files["redirect.txt"]
                if redirect not in (b"true\n", b"false\n") or redirect == b"true\n" and len(names) != 1:
                    raise ValueError("Invalid PHP redirect group")

                def relative(name: str) -> str:
                    prefix = source_root + "/"
                    if not name.startswith(prefix):
                        raise ValueError(f"PHP result outside source root: {name}")
                    name = name.removeprefix(prefix)
                    if (not name.endswith(".phpt") or str(PurePosixPath(name)) != name
                            or ".." in PurePosixPath(name).parts):
                        raise ValueError(f"Invalid PHP result path: {name}")
                    return name

                def suite_name(name: str) -> str:
                    directory = PurePosixPath(name).parent
                    # PHP searches at most five ancestors for its source-root
                    # header. Deeper paths use its relative-path fallback,
                    # which preserves underscores and other punctuation.
                    if len(directory.parts) >= 5:
                        return "php." + str(directory).replace("/", ".").replace("-", ".")
                    return "php" + "".join("." + re.sub(r"[^a-z0-9]+", ".", part, flags=re.I)
                                            for part in directory.parts)

                xml_names: dict[str, str] = {}
                redirected: list[str] = []
                text = files["results.tsv"].decode()
                if not text.endswith("\n"):
                    raise ValueError("PHP state output is empty or truncated")
                for line in text.splitlines():
                    match line.split("\t"):
                        case [state, identity] if state in states:
                            if identity.startswith("# "):
                                parent, separator, child = identity[2:].partition(": ")
                                if not separator or redirect != b"true\n" or relative(parent) != names[0]:
                                    raise ValueError("Unexpected PHP redirect context")
                                child = relative(child)
                                identity = "# " + names[0] + ": " + child
                                # PHP runs redirected children in the parent's
                                # directory and keys native JUnit by that path.
                                xml_name = str(PurePosixPath(names[0]).parent / PurePosixPath(child).name)
                                redirected.append(identity)
                            else:
                                identity = relative(identity)
                                if identity not in names:
                                    raise ValueError(f"Unselected PHP result: {identity}")
                                xml_name = identity
                            if identity in native or xml_name in xml_names:
                                raise ValueError(f"Duplicate PHP result identity: {identity}")
                            native[identity] = state
                            xml_names[xml_name] = identity
                            # Early SKIP/BORK paths precede PHP's redirect
                            # filename remapping. Either native path is valid,
                            # but exactly one XML case must own this identity.
                            if identity.startswith("# ") and state in ("SKIPPED", "BORKED") and child != xml_name:
                                if child in xml_names:
                                    raise ValueError("Ambiguous PHP redirect XML identity")
                                xml_names[child] = identity
                            key = group + "/" + identity
                            if state in ("FAILED", "WARNED", "LEAKED", "BORKED"):
                                failures.add(key)
                            elif state in ("XFAILED", "XLEAKED"):
                                expected_failures[key] = state
                        case _:
                            raise ValueError("Invalid PHP native state row")
                if redirected:
                    if names[0] in native or names[0] in xml_names:
                        raise ValueError("PHP redirect also has a direct result")
                    xml_names[names[0]] = names[0]
                elif set(native) != set(names):
                    raise ValueError("PHP command is missing native results")
                root = ET.fromstring(files["junit.xml"])
                suites = list(root)
                if root.tag != "testsuites" or any(suite.tag != "testsuite" for suite in suites):
                    raise ValueError("Invalid PHP native JUnit root")
                cases = [case for suite in suites for case in suite]
                if any(case.tag != "testcase" for case in cases):
                    raise ValueError("Invalid PHP native JUnit testcase")
                observed: set[str] = set()
                for case in cases:
                    matches = [name for name in xml_names if case.get("name", "").startswith(name + " (")
                               and case.get("name", "").endswith(")")]
                    if len(matches) != 1 or xml_names[matches[0]] in observed:
                        raise ValueError("Unexpected or duplicate PHP JUnit identity")
                    identity = xml_names[matches[0]]
                    observed.add(identity)
                    state = native.get(identity, "REDIR")
                    tags = [item.tag for item in case]
                    key = group + "/" + identity
                    if any(tag not in ("failure", "error", "skipped", "warning") for tag in tags):
                        raise ValueError("Unknown PHP JUnit result")
                    if any(tag in ("failure", "error", "warning") for tag in tags):
                        failures.add(key)
                    if state == "SKIPPED":
                        if tags != ["skipped"]:
                            raise ValueError("PHP skipped state disagrees with JUnit")
                        skipped[key] = case[0].text or ""
                    elif state in ("PASSED", "XFAILED", "XLEAKED", "REDIR") and tags:
                        raise ValueError("PHP positive state disagrees with JUnit")
                # PHP registers XML files only in startTimer. An early skip
                # or a redirect without SKIPIF can increment native counters
                # without registering a testcase. Bind every such named state
                # to its exact per-suite deficit; all other missing rows fail.
                suite_names = [suite.get("name", "") for suite in suites]
                if len(suite_names) != len(set(suite_names)):
                    raise ValueError("Duplicate PHP native JUnit suite")
                extra_tests = {suite.get("name", ""): int(suite.get("tests", "-1")) - len(suite) for suite in suites}
                extra_skips = {suite.get("name", ""): int(suite.get("skip", "-1"))
                               - sum(case.find("skipped") is not None for case in suite) for suite in suites}
                missing_rows: list[tuple[str, str, tuple[str, ...]]] = []
                for identity in set(xml_names.values()) - observed:
                    state = native.get(identity, "REDIR")
                    if state != "SKIPPED" and not (redirected and identity == names[0]):
                        raise ValueError("PHP native JUnit is missing results")
                    original = identity.partition(": ")[2] if identity.startswith("# ") else identity
                    paths = [original]
                    if identity.startswith("# "):
                        # Some early skips precede redirect remapping; missing
                        # EXTENSIONS/ENV skips follow it but still have no timer.
                        paths.append(str(PurePosixPath(names[0]).parent / PurePosixPath(original).name))
                    candidates = tuple(dict.fromkeys(suite_name(path) for path in paths))
                    missing_rows.append((identity, state, candidates))
                # Every two-suite choice shares this isolated redirect's parent
                # suite. Consume fixed rows first, then original child deficits
                # before the shared parent deficit; no identity is discarded.
                for identity, state, candidates in sorted(missing_rows, key=lambda row: len(row[2])):
                    suite = next((name for name in candidates if extra_tests.get(name, 0) > 0
                                  and (state != "SKIPPED" or extra_skips.get(name, 0) > 0)), None)
                    if suite is None:
                        raise ValueError("PHP counter-only state has no matching source-suite counters")
                    extra_tests[suite] -= 1
                    key = group + "/" + identity
                    counter_only[key] = state
                    if state == "SKIPPED":
                        extra_skips[suite] -= 1
                        # -W records the state, but an unregistered JUnit row
                        # cannot supply the native skip reason.
                        skipped[key] = ""
                if (any(extra_tests.values()) or any(extra_skips.values())
                        or int(root.get("tests", "-1")) != len(cases) + len(missing_rows)):
                    raise ValueError("PHP JUnit testcase count differs from native counters")
                for container in [root, *suites]:
                    descendants = list(container.iter("testcase"))
                    for attribute, tag in (("failures", "failure"), ("errors", "error"), ("skip", "skipped")):
                        if attribute == "skip" and container is not root:
                            continue  # Exact per-suite deficits were consumed above.
                        extra = sum(state == "SKIPPED" for _, state, _ in missing_rows) if attribute == "skip" else 0
                        if int(container.get(attribute, "-1")) != extra + sum(case.find(tag) is not None for case in descendants):
                            raise ValueError("PHP JUnit result counters disagree with testcases")
                if redirected:
                    redirects[group + "/" + names[0]] = redirected
                completed.update(names)
            except (ValueError, KeyError, ET.ParseError) as error:
                errors.append(f"{group}: {error}")
        # Each command may run repeatedly. All groups must cover the original
        # batch equally, and every execution marker must own distinct files.
        if set(selections) != set(selected) or len(set(selections.values())) != 1:
            errors.append("PHP command groups do not cover their original batch equally")
    if len(referenced) != len(set(referenced)) or set(referenced) != groups.keys():
        errors.append("PHP execution markers differ from retained native report groups")
    return {"expected": expected, "results": results, "redirects": redirects, "counter_only": counter_only,
            "errors": errors, "passed": sum(state == "PASSED" for group in results.values() for state in group.values()),
            "expected_failures": expected_failures, "skipped": skipped, "failures": sorted(failures),
            "missing": sorted(set(expected) - completed)}


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


def nodejs_results(log: Path, attempt: Attempt, reports: dict[tuple[Attempt, str], Path],
                   *, exact_ids: bool = False) -> dict[str, object]:
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
    # https://github.com/nodejs/node/blob/26e72d85a/tools/test.py#L338-L413
    # The legacy tag retains the v22 extension-stripping contract for original
    # evidence; current owners require the source-owned exact identity marker.
    for names, body in batches:
        normalized = {name if exact_ids else name.removesuffix(".js"): name for name in names}
        if len(normalized) != len(names):
            errors.append("Node selection has ambiguous normalized names")
        marker = False
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
                        r"(?:TAP version |1\.\.|(?:not )?ok |# gvisor-node-)", line):
                    errors.append(f"Unexpected Node output after footer: {line}")
            elif line == "# gvisor-node-exact-ids":
                if not exact_ids or marker or header or omitted:
                    errors.append("Unexpected Node exact-identity marker")
                marker = True
            elif line.startswith("# gvisor-node-skip "):
                # The image patch reports the canonical should_keep decision;
                # do not reconstruct the status-file classifier here.
                try:
                    match json.loads(line.removeprefix("# gvisor-node-skip ")):
                        case {"name": str(normalized_name), "reason": str(reason), **extra} if (
                                not extra and reason in ("--skip-tests", "status-file SKIP", "--flaky-tests=skip")):
                            name = normalized.get(normalized_name, normalized_name)
                            if (header or exact_ids and not marker or normalized_name not in normalized
                                    or name in omitted or name in results):
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
                if header or current is not None or exact_ids and not marker:
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
        if exact_ids and not marker:
            errors.append("Node batch has no exact-identity marker")
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
            "exact_ids": exact_ids,
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


def compact_records(path: Path) -> tuple[dict[int, dict[str, object]], list[dict[str, object]]]:
    """Read concatenated objects from `bb print --raw=true --sort=false`."""
    decoder = json.JSONDecoder()
    text = path.read_text()
    offset = 0
    records: dict[int, dict[str, object]] = {}
    spawns = []
    while offset < len(text):
        if text[offset].isspace():
            offset += 1
            continue
        raw, offset = decoder.raw_decode(text, offset)
        record = mapping(raw)
        if "spawn" in record:
            spawns.append(mapping(record["spawn"]))
        # ExecLogEntry.id identifies interned inputs. Invocation.id is a UUID,
        # not an input identity, and must not enter this index.
        if "id" not in record:
            continue
        identifier = record["id"]
        if type(identifier) is not int or identifier <= 0:
            raise ValueError(f"Invalid execution record ID: {identifier}")
        if identifier in records:
            raise ValueError(f"Duplicate execution record: {identifier}")
        records[identifier] = record
    return records, spawns


def input_chain(records: dict[int, dict[str, object]], start: int, target: int,
                producers: dict[int, list[dict[str, object]]] | None = None) -> list[int] | None:
    """Follow declared inputs, optionally through the actions producing them."""
    parents: dict[int, int | None] = {start: None}
    pending = deque([start])
    while pending:
        current = pending.popleft()
        if current not in records:
            raise ValueError(f"Missing execution input record: {current}")
        if current == target:
            path = []
            node: int | None = current
            while node is not None:
                path.append(node)
                node = parents[node]
            return list(reversed(path))
        record = records[current]
        inputs = mapping(record.get("inputSet", {}))
        edges = sequence(inputs.get("inputIds", [])) + sequence(inputs.get("transitiveSetIds", []))
        if "runfilesTree" in record:
            edges.append(mapping(record["runfilesTree"])["inputSetId"])
        if producers is not None and current in producers:
            match producers[current]:
                case [producer]:
                    if number(producer.get("exitCode", 0)) or producer.get("status"):
                        raise ValueError(f"Unsuccessful input producer: {producer.get('targetLabel')}")
                    edges.append(producer["inputSetId"])
                case _:
                    raise ValueError(f"Ambiguous input producer: {current}")
        for raw in edges:
            child = number(raw)
            if child not in parents:
                parents[child] = current
                pending.append(child)
    return None


def spawn_output_files(records: dict[int, dict[str, object]], spawn: dict[str, object]) -> dict[str, dict[str, object]]:
    """Index original output-file digests, including tree-artifact members."""
    files = {}
    for raw in sequence(spawn.get("outputs")):
        output = mapping(raw)
        if "outputId" not in output:
            continue
        record = records[number(output["outputId"])]
        if "file" in record:
            entries = [mapping(record["file"])]
        elif "directory" in record:
            directory = mapping(record["directory"])
            entries = [mapping(file) | {"path": string(directory["path"]) + "/" + string(mapping(file)["path"])}
                       for file in sequence(directory.get("files", []))]
        else:
            continue
        for entry in entries:
            path = string(entry["path"])
            if path in files:
                raise ValueError(f"Duplicate output file: {path}")
            digest = mapping(entry.get("digest"))
            files[path] = {"sha256": string(digest["hash"]), "bytes": number(digest.get("sizeBytes", 0))}
    return files


def image_command_result(attempt: Attempt, reports: dict[tuple[Attempt, str], Path],
                         command: dict[str, object]) -> dict[str, object]:
    """Read one image-build shard; the caller must prove its source ancestry."""
    prefix = "source-images/"
    files: dict[str, bytes] = {}
    if archive_path := reports.get((attempt, "test.outputs__outputs.zip")):
        with zipfile.ZipFile(archive_path) as archive:
            total = 0
            for item in archive.infolist():
                if not item.filename.startswith(prefix) or item.is_dir():
                    continue
                total += item.file_size
                if item.filename in files or item.file_size > MAX_REPORT_BYTES or total > MAX_TOTAL_REPORT_BYTES:
                    raise ValueError("Duplicate or oversized image-build result")
                files[item.filename] = archive.read(item)
    else:
        files = {name.removeprefix("test.outputs/"): path.read_bytes()
                 for (owner, name), path in reports.items()
                 if owner == attempt and name.startswith("test.outputs/" + prefix)}
    selection = mapping(json.loads(files[prefix + "selection.json"]))
    if (set(selection) != {"schema_version", "architecture", "image_class", "partition", "partitions", "cohort", "selected", "images"}
            or type(selection["schema_version"]) is not int or selection["schema_version"] != 1
            or selection["architecture"] != command["architecture"] or selection["image_class"] != command["image_class"]
            or type(selection["partition"]) is not int or type(selection["partitions"]) is not int
            or selection["partition"] != attempt.shard or not 1 <= selection["partition"] <= selection["partitions"]):
        raise ValueError("Image-build selection differs from its configured command or shard")
    names = {}
    for key in ("cohort", "selected", "images"):
        values = [string(name) for name in sequence(selection[key])]
        if (len(values) != len(set(values)) or key != "images" and not values
                or any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) is None for name in values)):
            raise ValueError(f"Invalid or duplicate image names: {key}")
        names[key] = set(values)
    selected = set(sequence(command["images"])) or names["cohort"]
    if names["selected"] != selected or not names["images"] <= selected <= names["cohort"]:
        raise ValueError("Image-build selection differs from its declared cohort")
    expected_files = {prefix + "selection.json"} | {
        prefix + image + "/" + name for image in names["images"]
        for name in ("request.json", "build-exit-code", "inspect.json", "exit-code")
    }
    if files.keys() != expected_files:
        raise ValueError("Image-build results are missing or unexpected")
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}[string(command["architecture"])]
    results = []
    for image in sequence(selection["images"]):
        path = prefix + string(image) + "/"
        request = mapping(json.loads(files[path + "request.json"]))
        if (set(request) != {"schema_version", "image", "architecture", "operation", "reference", "archive_sha256"}
                or type(request["schema_version"]) is not int or request["schema_version"] != 1
                or request["image"] != image or request["architecture"] != command["architecture"]
                or request["operation"] not in ("docker-build", "archive-load")):
            raise ValueError(f"Image-build request differs from its selection: {image}")
        archive = request["archive_sha256"]
        if (request["operation"] == "docker-build" and archive is not None
                or request["operation"] == "archive-load" and re.fullmatch(r"[0-9a-f]{64}", string(archive)) is None):
            raise ValueError(f"Invalid image-build archive identity: {image}")
        if any(files[path + name].strip() != b"0" for name in ("build-exit-code", "exit-code")):
            raise ValueError(f"Image build or result collection failed: {image}")
        observations = sequence(json.loads(files[path + "inspect.json"]))
        if len(observations) != 1:
            raise ValueError(f"Expected one built image: {image}")
        observed = mapping(observations[0])
        reference = string(request["reference"])
        image_name = string(image).replace("_", "/")
        if (re.fullmatch(r"gvisor\.dev/images/" + re.escape(image_name) + r":[0-9a-f]{16}", reference) is None
                or reference not in sequence(observed.get("RepoTags"))
                or observed.get("Os") != "linux" or observed.get("Architecture") != architecture
                or re.fullmatch(r"sha256:[0-9a-f]{64}", string(observed.get("Id"))) is None):
            raise ValueError(f"Built image identity or platform differs: {image}")
        results.append({"image": image_name, "architecture": architecture, "operation": request["operation"],
                        "archive_sha256": archive, "reference": reference, "image_id": observed["Id"]})
    return {**selection, "results": results,
            "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                      for name, data in files.items()}}


def image_command_partitions(rows: list[dict[str, object]]) -> None:
    """Require every selected image exactly once across one run's shards."""
    if not rows:
        raise ValueError("Missing image-build shards")
    first = rows[0]
    shared = ("architecture", "image_class", "partitions", "cohort", "selected")
    if (number(first["partitions"]) != len(rows)
            or any(any(row[key] != first[key] for key in shared) for row in rows)
            or Counter(number(row["partition"]) for row in rows) != Counter(range(1, len(rows) + 1))
            or Counter(string(image) for row in rows for image in sequence(row["images"])) != Counter(sequence(first["selected"]))):
        raise ValueError("Image-build shards differ from the complete selected cohort")


def declared_source_images(value: object) -> list[dict[str, object]]:
    """Validate image origins without changing the original schema-1 source form."""
    images = [mapping(image) for image in sequence(value)]
    for image in images:
        fields = {"name", "architecture", "archive"}
        if image.keys() not in (fields, fields | {"origin", "layout"},
                               fields | {"origin", "layout", "usage", "config", "pin", "index"}):
            raise ValueError(f"Invalid configured image fields: {image}")
        if (not string(image["name"]) or not string(image["archive"])
                or image["architecture"] not in ("amd64", "arm64")):
            raise ValueError(f"Invalid configured image obligation: {image}")
        origin, layout = image.get("origin", "source"), string(image.get("layout", ""))
        if origin not in ("source", "native-oci", "registry") or (origin == "source") != (not layout):
            raise ValueError(f"Invalid configured image origin/layout: {image}")
        usage, config = image.get("usage", "docker-load"), string(image.get("config", ""))
        if usage not in ("docker-load", "cri-import", "staged-only") or (usage == "docker-load") != (not config):
            raise ValueError(f"Invalid configured image usage/config: {image}")
        if ((origin == "registry") != bool(string(image.get("pin", "")))
                or (origin == "registry") != bool(string(image.get("index", "")))):
            raise ValueError(f"Registry dependency requires its configured pin: {image['name']}")
    if len(images) != len({(image["name"], image["architecture"], image["archive"]) for image in images}):
        raise ValueError("Duplicate configured image obligations")
    return images


def source_image_audit(execution: Path, canonical: Path, manifest: Path,
                       expectations: Path) -> dict[str, object]:
    """Audit one invocation without replacing its workload verdict.

    The caller authenticates the containing bundle with receipt(), selects each
    invocation explicitly, and decodes its compact log. Expectations contain an
    owners mapping from target labels to complete image lists, plus optional
    benchmarks in the existing declared-benchmark format. Configured source
    declarations bind each image to its architecture and archive; build-only
    audits require those declarations and cannot establish runtime loads.
    Obligations are never inferred from successful observed loads.
    """
    verdict: dict[str, object] = {}
    errors: list[str] = []
    inputs = []
    images: list[dict[str, object]] = []
    consumers: list[dict[str, object]] = []
    try:
        verdict = read_json(canonical)
        for path in (execution, canonical, manifest, expectations):
            inputs.append({"path": str(path), **file_record(path)})
        if verdict.get("status") not in ("PASS", "FAIL", "INCOMPLETE"):
            raise ValueError("Missing canonical qualification status")
        if verdict.get("command_kind") not in ("test", "benchmark", "build"):
            raise ValueError("Source-image audit requires a build or test invocation")
        contract = read_json(expectations)
        if set(contract) - {"owners", "benchmarks", "configured_owners"}:
            raise ValueError("Unexpected source-image expectation fields")
        wanted = {label: [string(image) for image in sequence(raw)]
                  for label, raw in mapping(contract.get("owners")).items()}
        if not wanted or any(len(rows) != len(set(rows)) for rows in wanted.values()):
            raise ValueError("Expected unique source images for every owner")
        expected = [Owner(string(mapping(row).get("label")), string(mapping(row).get("configuration")))
                    for row in sequence(verdict.get("expected"))]
        if len(expected) != len(set(expected)) or set(wanted) != {owner.label for owner in expected}:
            raise ValueError("Source-image owners differ from canonical configured owners")
        configured_images: dict[Owner, list[dict[str, object]]] = {}
        configured_commands: dict[Owner, dict[str, object]] = {}
        producer_images: dict[Owner, list[dict[str, object]]] = {}
        configured_pins: dict[str, str] = {}
        configured_indexes: dict[str, dict[str, object]] = {}
        if "configured_owners" in contract:
            for raw in sequence(contract["configured_owners"]):
                row = mapping(raw)
                owner = Owner(string(row.get("label")), string(row.get("configuration")))
                if owner in configured_images:
                    raise ValueError(f"Duplicate configured image owner: {owner}")
                configured_images[owner] = declared_source_images(row.get("images"))
                inventory = list(configured_images[owner])
                commands = sequence(row.get("source_commands"))
                if commands:
                    if len(commands) != 1 or not mapping(commands[0]).get("contexts"):
                        raise ValueError(f"Dynamic source-image commands require their own load audit with configured contexts: {owner}")
                    command = mapping(commands[0])
                    configured_commands[owner] = command
                    inventory.extend(declared_source_images(command["context_images"]))
                producer_images[owner] = inventory
                pins = mapping(row.get("pins", {}))
                if pins.keys() != {image["pin"] for image in inventory if image.get("origin") == "registry"}:
                    raise ValueError(f"Registry pin evidence differs from declared images: {owner}")
                for path, raw_pin in pins.items():
                    pin = mapping(raw_pin)
                    digest = string(pin.get("digest"))
                    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                        raise ValueError(f"Invalid configured registry pin: {path}")
                    if path in configured_pins and configured_pins[path] != digest:
                        raise ValueError(f"Conflicting configured registry pin: {path}")
                    configured_pins[path] = digest
                indexes = mapping(row.get("indexes", {}))
                if indexes.keys() != {image["index"] for image in inventory if image.get("origin") == "registry"}:
                    raise ValueError(f"Registry index evidence differs from declared images: {owner}")
                for path, raw_index in indexes.items():
                    index = mapping(raw_index)
                    body = string(index.get("body")).encode()
                    evidence = mapping(index.get("evidence"))
                    if (hashlib.sha256(body).hexdigest(), len(body)) != (evidence.get("sha256"), evidence.get("bytes")):
                        raise ValueError(f"Registry index bytes differ from retained evidence: {path}")
                    if path in configured_indexes and configured_indexes[path] != index:
                        raise ValueError(f"Conflicting configured registry index: {path}")
                    configured_indexes[path] = index
            if configured_images.keys() != set(expected):
                raise ValueError("Image declarations differ from configured owners")
            for label, names in wanted.items():
                if set(names) != {image["name"] for owner, rows in configured_images.items()
                                  if owner.label == label for image in rows}:
                    raise ValueError(f"Image name summary differs from configured declarations: {label}")
        elif any(not rows for rows in wanted.values()):
            raise ValueError("Empty image obligations require configured source declarations")
        if verdict["command_kind"] == "build" and not configured_images:
            raise ValueError("Build image audits require configured archive declarations")
        if verdict["command_kind"] == "build" and configured_commands:
            raise ValueError("Image-build tests must execute to qualify their selected images")
        required_benchmarks = benchmark_expectations(contract["benchmarks"]) if "benchmarks" in contract else {}
        if not set(required_benchmarks) <= set(wanted):
            raise ValueError("Benchmark obligations name an unexpected source-image owner")
        if verdict["command_kind"] == "build" and required_benchmarks:
            raise ValueError("Build image audits cannot satisfy benchmark obligations")
        test_owners = set() if verdict["command_kind"] == "build" else {
            owner for owner in expected
            if configured_images.get(owner, wanted[owner.label]) or owner in configured_commands or owner.label in required_benchmarks
        }
        test_labels = {owner.label for owner in test_owners}
        _, reports, report_errors = retained_reports(manifest)
        errors.extend(report_errors)
        records, spawns = compact_records(execution)

        def outputs(spawn: dict[str, object]) -> list[int]:
            return [number(mapping(output)["outputId"]) for output in sequence(spawn.get("outputs"))
                    if "outputId" in mapping(output)]

        producers: dict[int, list[dict[str, object]]] = {}
        for spawn in spawns:
            for identifier in outputs(spawn):
                producers.setdefault(identifier, []).append(spawn)

        def chains(spawn: dict[str, object], identifiers: list[int]) -> list[list[int]] | None:
            paths = []
            for identifier in identifiers:
                if (path := input_chain(records, number(spawn["inputSetId"]), identifier)) is None:
                    return None
                paths.append(path)
            return paths

        def successful(spawn: dict[str, object]) -> None:
            if number(spawn.get("exitCode", 0)) or spawn.get("status"):
                raise ValueError(f"Unsuccessful source/archive action: {spawn.get('targetLabel')}")

        def identity(spawn: dict[str, object], *, executed: bool = False) -> dict[str, object]:
            runner = string(spawn.get("runner"))
            if executed and (runner not in ("remote", "docker", "local", "linux-sandbox")
                             or runner == "remote" and not spawn.get("digest")):
                raise ValueError(f"Missing execution identity: {spawn.get('targetLabel')}: {runner}")
            return {key: spawn.get(key) for key in ("targetLabel", "inputSetId", "runner", "digest")} | {
                "exit_code": number(spawn.get("exitCode", 0)), "cache_hit": spawn.get("cacheHit", False),
                "remote_cache_hit": spawn.get("remoteCacheHit", False)}

        def archive_outputs(image: str, architecture: str, source_outputs: list[int],
                            producer: dict[str, object], origin: str, layout: str = "") -> None:
            manifests = [spawn for spawn in spawns if spawn.get("mnemonic") == "OCITarballManifest"
                         and chains(spawn, source_outputs) is not None]
            if not manifests:
                raise ValueError(f"Missing archive manifest for {image}")
            # Runtime and image-context consumers can share a producer while
            # retaining separately configured stock archive conversions.
            for conversion in manifests:
                successful(conversion)
                conversion_outputs = outputs(conversion)
                if not conversion_outputs:
                    raise ValueError(f"Missing archive manifest outputs: {image}")
                archives = [spawn for spawn in spawns if spawn.get("mnemonic") == "OCITarball"
                            and chains(spawn, source_outputs + conversion_outputs) is not None]
                match archives:
                    case [archive]:
                        successful(archive)
                    case _:
                        raise ValueError(f"Expected one archive action for {image}, got {len(archives)}")
                match outputs(archive):
                    case [archive_id]:
                        archive_file = mapping(records[archive_id].get("file"))
                    case _:
                        raise ValueError(f"Expected one archive output for {image}")
                digest = mapping(archive_file.get("digest"))
                if (re.fullmatch(r"[0-9a-f]{64}", string(digest.get("hash"))) is None
                        or number(digest.get("sizeBytes", 0)) <= 0):
                    raise ValueError(f"Missing archive identity: {image}")
                architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(architecture, architecture)
                if architecture not in ("amd64", "arm64"):
                    raise ValueError(f"Unsupported source-image architecture: {architecture}")
                images.append({"image": image, "architecture": architecture,
                               "origin": origin, "producer": producer, "archive_action": identity(archive),
                               "layout": layout, "archive_id": archive_id, "archive": archive_file,
                               "manifest_inputs": chains(conversion, source_outputs),
                               "archive_inputs": chains(archive, source_outputs + conversion_outputs)})

        for source in (spawn for spawn in spawns if spawn.get("mnemonic") == "SourceImageOCI"):
            successful(source)
            if source.get("cacheHit", False) or source.get("remoteCacheHit", False):
                raise ValueError(f"Source image was cached: {source.get('targetLabel')}")
            match sequence(source.get("args")):
                case [_, "test/docker/source_images.sh", "layout", str(architecture), _, _, _, _, str(image), *_]:
                    pass
                case _:
                    raise ValueError("Unsupported source-image command")
            source_outputs = outputs(source)
            if not source_outputs:
                raise ValueError(f"Missing source outputs: {image}")
            archive_outputs(image, architecture, source_outputs, identity(source, executed=True), "source")

        native_layouts: dict[tuple[str, str, str], str] = {}
        registry_layouts: dict[tuple[str, str, str, str], set[str]] = {}
        for declarations in producer_images.values():
            for declared in declarations:
                origin = declared.get("origin", "source")
                if origin == "registry":
                    key = (string(declared["name"]), string(declared["architecture"]),
                           string(declared["layout"]), configured_pins[string(declared["pin"])])
                    registry_layouts.setdefault(key, set()).add(string(declared["index"]))
                    continue
                if origin != "native-oci":
                    continue
                key = (string(declared["name"]), string(declared["architecture"]), string(declared["archive"]))
                layout = string(declared["layout"])
                if key in native_layouts and native_layouts[key] != layout:
                    raise ValueError(f"Conflicting native OCI layouts for {key}")
                native_layouts[key] = layout
        records_by_path: dict[tuple[str, str], list[tuple[int, dict[str, object]]]] = {}
        for identifier, record in records.items():
            for kind in ("directory", "file"):
                if kind in record:
                    records_by_path.setdefault((kind, string(mapping(record[kind])["path"])), []).append((identifier, record))
        # The same configured layout can feed several stock conversions or owners.
        for image, architecture, layout in sorted({(name, architecture, layout)
                for (name, architecture, _), layout in native_layouts.items()}):
            match records_by_path.get(("directory", layout), []):
                case [(layout_id, layout_record)]:
                    pass
                case _:
                    raise ValueError(f"Missing or ambiguous native OCI layout: {layout}")
            match [spawn for spawn in spawns if spawn.get("mnemonic") == "OCIImage" and layout_id in outputs(spawn)]:
                case [source]:
                    successful(source)
                case _:
                    raise ValueError(f"Missing unique native OCI assembly: {layout}")
            native_inputs = []
            for argument in sequence(source.get("args")):
                argument = string(argument)
                if argument.startswith("--from="):
                    paths = [("base", argument.removeprefix("--from="))]
                elif argument.startswith("--layer="):
                    match argument.removeprefix("--layer=").split("=", 1):
                        case [layer, descriptor] if layer and descriptor:
                            paths = [("layer", layer), ("descriptor", descriptor)]
                        case _:
                            raise ValueError(f"Invalid native OCI layer argument: {argument}")
                else:
                    continue
                for kind, path in paths:
                    match records_by_path.get(("directory" if kind == "base" else "file", path), []):
                        case [(identifier, record)]:
                            pass
                        case _:
                            raise ValueError(f"Missing unique native OCI {kind} input: {path}")
                    if (chain := input_chain(records, number(source["inputSetId"]), identifier)) is None:
                        raise ValueError(f"Native OCI {kind} is not an assembly input: {path}")
                    native_inputs.append({"kind": kind, "input": record, "input_chain": chain})
            if not native_inputs:
                raise ValueError(f"Missing native OCI assembly inputs: {layout}")
            cached = bool(source.get("cacheHit", False) or source.get("remoteCacheHit", False))
            if cached and not source.get("digest"):
                raise ValueError(f"Missing cached native OCI action identity: {layout}")
            producer = identity(source, executed=not cached) | {"inputs": native_inputs, "output": layout_record}
            archive_outputs(image, architecture, [layout_id], producer, "native-oci", layout)
        for (image, architecture, layout, pin), index_paths in sorted(registry_layouts.items()):
            match records_by_path.get(("directory", layout), []):
                case [(layout_id, layout_record)]:
                    pass
                case _:
                    raise ValueError(f"Missing or ambiguous registry layout: {layout}")
            match [spawn for spawn in spawns if spawn.get("mnemonic") == "CopyToDirectory" and layout_id in outputs(spawn)]:
                case [source]:
                    successful(source)
                case _:
                    raise ValueError(f"Missing unique registry layout copy: {layout}")
            match sequence(source.get("args")):
                case [_, _, str(repository)] if repository and "/" not in repository:
                    pass
                case _:
                    raise ValueError(f"Unsupported registry layout copy: {layout}")
            if not layout.endswith("/bin/external/" + repository + "/layout"):
                raise ValueError(f"Registry layout differs from its declared repository: {layout}")
            retained_indexes = [configured_indexes[path] for path in sorted(index_paths)]
            if len({string(index["body"]) for index in retained_indexes}) != 1:
                raise ValueError(f"Configured copies of the registry index differ: {layout}")
            retained_index = retained_indexes[0]
            index = mapping(json.loads(string(retained_index["body"])))
            match sequence(index.get("manifests")):
                case [{"digest": str(selected), "mediaType": str(media_type), "size": int(size)}] if (
                        index.get("schemaVersion") == 2 and selected == pin and size > 0
                        and media_type in ("application/vnd.oci.image.manifest.v1+json",
                                           "application/vnd.docker.distribution.manifest.v2+json")):
                    pass
                case _:
                    raise ValueError(f"Registry index does not select the configured manifest pin: {pin}")
            copied = []
            files = [mapping(file) for file in sequence(mapping(layout_record["directory"]).get("files"))]
            match [mapping(file.get("digest")) for file in files if file.get("path") == "index.json"]:
                case [{"hash": str(digest), "sizeBytes": raw_size}] if (
                        digest == mapping(retained_index["evidence"])["sha256"]
                        and number(raw_size) == mapping(retained_index["evidence"])["bytes"]):
                    pass
                case _:
                    raise ValueError(f"Registry index differs from the configured layout: {layout}")
            paths = [string(file.get("path")) for file in files]
            if len(paths) != len(set(paths)) or not {"index.json", "oci-layout"} <= set(paths):
                raise ValueError(f"Incomplete registry layout: {layout}")
            pinned = "blobs/sha256/" + pin.removeprefix("sha256:")
            if not any(file["path"] == pinned and number(mapping(file["digest"])["sizeBytes"]) == size for file in files):
                # An index pin is not the selected platform's manifest pin.
                # Do not infer an index-to-child relationship from a pull name.
                raise ValueError(f"Registry pin is not a retained layout manifest: {pin}")
            for file in files:
                path = string(file["path"])
                digest = mapping(file.get("digest"))
                if (path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/"))
                        or re.fullmatch(r"[0-9a-f]{64}", string(digest.get("hash"))) is None
                        or number(digest.get("sizeBytes", 0)) <= 0
                        or path.startswith("blobs/sha256/") and path != "blobs/sha256/" + digest["hash"]):
                    raise ValueError(f"Invalid registry layout member: {path}")
                match records_by_path.get(("file", "external/" + repository + "/" + path), []):
                    case [(identifier, record)] if mapping(record["file"]).get("digest") == digest:
                        pass
                    case _:
                        raise ValueError(f"Registry layout member differs from its pull input: {path}")
                if (chain := input_chain(records, number(source["inputSetId"]), identifier)) is None:
                    raise ValueError(f"Registry member is not a layout copy input: {path}")
                copied.append({"input": record, "input_chain": chain})
            cached = bool(source.get("cacheHit", False) or source.get("remoteCacheHit", False))
            if cached and not source.get("digest"):
                raise ValueError(f"Missing cached registry copy identity: {layout}")
            producer = identity(source, executed=not cached) | {"pin": pin, "indexes": retained_indexes, "inputs": copied, "output": layout_record}
            archive_outputs(image, architecture, [layout_id], producer, "registry", layout)
        required_images = ({image["name"] for rows in producer_images.values() for image in rows}
                           if configured_images else {image for rows in wanted.values() for image in rows})
        if {row["image"] for row in images} != required_images:
            raise ValueError("Source producers differ from declared image obligations")
        if configured_images:
            declared_archives = {(image["name"], image["architecture"], image["archive"])
                                 for rows in producer_images.values() for image in rows}
            actual_archives = {(row["image"], row["architecture"], mapping(row["archive"])["path"]) for row in images}
            if actual_archives != declared_archives:
                raise ValueError("Source archive identities differ from configured declarations")
        expected_attempts = []
        for raw in sequence(verdict.get("executed")):
            row = mapping(raw)
            owner = mapping(row["owner"])
            if Owner(string(owner["label"]), string(owner["configuration"])) in test_owners:
                expected_attempts.append(row)
        attempts = {Attempt(Owner(string(mapping(row["owner"])["label"]), string(mapping(row["owner"])["configuration"])),
                            *(number(row[key]) for key in ("run", "shard", "attempt"))) for row in expected_attempts}
        if (test_owners and not attempts) or len(attempts) != len(expected_attempts):
            raise ValueError("Missing or duplicate canonical test attempts")
        if {attempt.owner for attempt in attempts} != test_owners or any(
                min(attempt.run, attempt.shard, attempt.attempt) < 1 for attempt in attempts):
            raise ValueError("Executed attempts differ from configured owners")
        observations = {Attempt(Owner(string(mapping(row["owner"])["label"]), string(mapping(row["owner"])["configuration"])),
                                *(number(row[key]) for key in ("run", "shard", "attempt"))): row
                        for row in expected_attempts}
        logs = {str(path.relative_to(manifest.parent.resolve())): attempt
                for (attempt, name), path in reports.items() if name == "test.log"}
        observed: set[Attempt] = set()
        image_builds: dict[tuple[Owner, int, int], list[dict[str, object]]] = {}
        for test in (spawn for spawn in spawns if spawn.get("mnemonic") == "TestRunner"):
            if test.get("targetLabel") not in test_labels:
                continue
            if sequence(test.get("args"))[:1] == ["external/bazel_tools/tools/test/generate-xml.sh"]:
                continue
            files = [mapping(records[identifier].get("file", {})) for identifier in outputs(test)]
            xml_files = [file for file in files if string(file.get("path", "")).endswith("/test.xml")]
            if not xml_files:
                # Some wrappers produce only stdout. Bind Bazel's generated
                # XML to this exact test log, not another shard of its label.
                match [mapping(output)["invalidOutputPath"] for output in sequence(test.get("outputs"))
                       if string(mapping(output).get("invalidOutputPath", "")).endswith("/test.xml")]:
                    case [str(xml_path)]:
                        pass
                    case _:
                        raise ValueError(f"Missing TestRunner XML identity: {test.get('targetLabel')}")
                generators = [spawn for spawn in spawns if spawn.get("mnemonic") == "TestRunner"
                              and spawn.get("targetLabel") == test.get("targetLabel")
                              and sequence(spawn.get("args"))[:3] == ["external/bazel_tools/tools/test/generate-xml.sh",
                                                                     xml_path.removesuffix("test.xml") + "test.log", xml_path]]
                match generators:
                    case [generator]:
                        successful(generator)
                        xml_files = [mapping(records[identifier].get("file", {})) for identifier in outputs(generator)
                                     if mapping(records[identifier].get("file", {})).get("path") == xml_path]
                    case _:
                        raise ValueError(f"Missing unique XML generator for {xml_path}")
            match xml_files:
                case [xml_file]:
                    log_path = "reports/" + string(xml_file["path"]).removeprefix("bazel-out/").removesuffix("test.xml") + "test.log"
                case _:
                    raise ValueError(f"Expected one TestRunner XML: {test.get('targetLabel')}")
            if log_path not in logs:
                raise ValueError(f"TestRunner log is not a captured attempt: {log_path}")
            attempt = logs[log_path]
            if attempt.owner not in test_owners:
                continue
            if attempt not in attempts or attempt in observed or test.get("targetLabel") != attempt.owner.label:
                raise ValueError(f"Duplicate or mismatched configured TestRunner: {attempt}")
            observed.add(attempt)
            execution_identity = identity(test, executed=True)
            if (execution_identity["runner"] != mapping(observations[attempt].get("execution_info")).get("strategy")
                    or execution_identity["cache_hit"] or execution_identity["remote_cache_hit"]):
                raise ValueError(f"TestRunner strategy/cache differs from uncached execution: {attempt}")
            log = reports[attempt, "test.log"]
            digest = mapping(xml_file.get("digest"))
            if file_record(reports[attempt, "test.xml"]) != {"sha256": digest.get("hash"), "bytes": number(digest.get("sizeBytes", 0))}:
                raise ValueError(f"TestRunner XML differs from captured bytes: {attempt}")
            image_build = None
            if attempt.owner in configured_commands:
                command = configured_commands[attempt.owner]
                image_build = image_command_result(attempt, reports, command)
                output_files = spawn_output_files(records, test)
                output_root = str(Path(string(xml_file["path"])).parent / "test.outputs") + "/"
                if zipped := reports.get((attempt, "test.outputs__outputs.zip")):
                    captured_files = {"outputs.zip": file_record(zipped)}
                else:
                    captured_files = mapping(image_build["files"])
                for name, digest in captured_files.items():
                    if output_files.get(output_root + name) != digest:
                        raise ValueError(f"Image-build result differs from TestRunner output: {attempt}: {name}")
                context_path = string(command["contexts"])
                match records_by_path.get(("file", context_path), []):
                    case [(context_id, context_record)]:
                        pass
                    case _:
                        raise ValueError(f"Missing configured image context: {context_path}")
                context_digest = mapping(mapping(context_record["file"]).get("digest"))
                if (re.fullmatch(r"[0-9a-f]{64}", string(context_digest.get("hash"))) is None
                        or number(context_digest.get("sizeBytes", 0)) <= 0
                        or (context_chain := input_chain(records, number(test["inputSetId"]), context_id)) is None):
                    raise ValueError(f"Configured image context is not a TestRunner input: {context_path}")
                image_build["context"] = {"input": context_record, "input_chain": context_chain}
                for result in sequence(image_build["results"]):
                    result = mapping(result)
                    declarations = [mapping(image) for image in sequence(command["context_images"])
                                    if (mapping(image)["name"], mapping(image)["architecture"]) ==
                                    (result["image"], result["architecture"])]
                    if not declarations:
                        if result["operation"] != "docker-build":
                            raise ValueError(f"Loaded archive is absent from configured context: {result['image']}")
                        continue
                    if len(declarations) != 1 or result["operation"] != "archive-load":
                        raise ValueError(f"Image operation differs from configured context: {result['image']}")
                    declaration = declarations[0]
                    match [image for image in images if (image["image"], image["architecture"], mapping(image["archive"])["path"]) ==
                           (declaration["name"], declaration["architecture"], declaration["archive"])]:
                        case [image]:
                            pass
                        case _:
                            raise ValueError(f"Missing unique context archive producer: {result['image']}")
                    if mapping(mapping(image["archive"])["digest"])["hash"] != result["archive_sha256"]:
                        raise ValueError(f"Loaded archive differs from configured context: {result['image']}")
                    if (chain := input_chain(records, context_id, number(image["archive_id"]), producers)) is None:
                        raise ValueError(f"Archive is not a context input: {result['image']}")
                    result["archive"] = image["archive"]
                    result["input_chain"] = chain
                image_builds.setdefault((attempt.owner, attempt.run, attempt.attempt), []).append(image_build)
            text = log.read_text()
            declared = configured_images.get(attempt.owner, [])
            cri = [image for image in declared if image.get("usage", "docker-load") != "docker-load"]
            cri_evidence = None
            imported: Counter[str] = Counter()
            if cri:
                match sorted({string(image["config"]) for image in cri}):
                    case [config_path]:
                        pass
                    case _:
                        raise ValueError(f"Expected one declared CRI configuration: {attempt}")
                match config_path.split("/bin/", 1):
                    case [_, config_short]:
                        pass
                    case _:
                        raise ValueError(f"Unsupported CRI config path: {config_path}")
                if sequence(test.get("args")).count("--harness_image_config=" + config_short) != 1:
                    raise ValueError(f"TestRunner does not select its declared CRI configuration: {attempt}")
                observations_cri = [mapping(json.loads(body)) for body in re.findall(
                    r"Staged declared CRI images: ([^\r\n]+)", text)]
                match observations_cri:
                    case [{"path": str(path), "config": str(body)}] if path == config_short:
                        config_bytes = body.encode()
                    case _:
                        raise ValueError(f"Missing unique staged CRI configuration: {attempt}")
                config = mapping(json.loads(config_bytes))
                configured_paths = {string(image["name"]): string(image["archive"]).split("/bin/", 1)[-1] for image in cri}
                if (config.keys() != {"containerd_version", "sandbox_image", "images"}
                        or mapping(config["images"]) != configured_paths
                        or config.get("sandbox_image") not in configured_paths
                        or sequence(test.get("args")).count("--containerd_version=" + string(config["containerd_version"])) != 1):
                    raise ValueError(f"Staged CRI configuration differs from declared archives/version: {attempt}")
                match records_by_path.get(("file", config_path), []):
                    case [(config_id, record)]:
                        pass
                    case _:
                        raise ValueError(f"Missing unique CRI configuration input: {config_path}")
                digest = mapping(mapping(record["file"]).get("digest"))
                if (digest.get("hash") != hashlib.sha256(config_bytes).hexdigest()
                        or number(digest.get("sizeBytes", 0)) != len(config_bytes)
                        or (config_chain := input_chain(records, number(test["inputSetId"]), config_id)) is None):
                    raise ValueError(f"Staged CRI config bytes differ from TestRunner input: {attempt}")
                for name, path in re.findall(r"Imported declared CRI image (\S+) from (\S+)", text):
                    if name not in configured_paths or path != "/cri-images/" + name.replace("/", "_") + ".tar":
                        raise ValueError(f"Unexpected checked CRI import: {name}: {path}")
                    imported[name] += 1
                required_imports = {string(image["name"]) for image in cri if image["usage"] == "cri-import"}
                if imported.keys() != required_imports:
                    raise ValueError(f"Checked CRI imports differ from required imports: {attempt}")
                cri_evidence = {"config": record, "input_chain": config_chain, "imports": dict(imported)}
            loaded = []
            for image in images:
                if (chain := input_chain(records, number(test["inputSetId"]), number(image["archive_id"]))) is None:
                    continue
                match string(mapping(image["archive"])["path"]).split("/bin/", 1):
                    case [_, archive_path]:
                        pass
                    case _:
                        raise ValueError(f"Unsupported archive output path: {image['archive']}")
                declaration = next((item for item in declared if (item["name"], item["architecture"], item["archive"]) ==
                                    (image["image"], image["architecture"], mapping(image["archive"])["path"])), None)
                usage = declaration.get("usage", "docker-load") if declaration else "docker-load"
                row = {"image": image["image"], "architecture": image["architecture"], "archive": image["archive"],
                       "input_chain": chain, "usage": usage}
                if usage == "docker-load":
                    blocks = [body for path, body in re.findall(
                        r"Loaded declared image archive ([^\r\n]+):\r?\n((?:Loaded image: [^\r\n]+\r?\n)+)", text)
                              if path == archive_path]
                    tags = sorted({tag for body in blocks for tag in re.findall(r"Loaded image: (\S+)", body)
                                   if tag.startswith(f"gvisor.dev/images/{image['image']}:")})
                    if len(blocks) != 1 or not tags:
                        raise ValueError(f"Missing actual archive load for {attempt}: {image['image']}")
                    row["tags"] = tags
                elif usage == "cri-import":
                    row["imports"] = imported[string(image["image"])]
                loaded.append(row)
            if configured_images:
                actual = Counter((row["image"], row["architecture"], mapping(row["archive"])["path"]) for row in loaded)
                required = Counter((row["name"], row["architecture"], row["archive"]) for row in configured_images[attempt.owner])
                if actual != required:
                    raise ValueError(f"Loaded source archives differ from configured obligations for {attempt}")
            elif Counter(row["image"] for row in loaded) != Counter(wanted[attempt.owner.label]):
                raise ValueError(f"Loaded source images differ from obligations for {attempt}")
            if attempt.owner.label in required_benchmarks:
                # Reuse the canonical parser, including its rejection of
                # partial measurements in a failed or unfinished package.
                rows = benchmark_rows(log)
                leaves = required_benchmarks[attempt.owner.label]
                wanted_names = {leaf.name for leaf in leaves}
                names = [string(row["name"]) if row["name"] in wanted_names
                         else re.sub(r"-\d+$", "", string(row["name"])) for row in rows]
                if Counter(names) != Counter({leaf.name: leaf.count for leaf in leaves}):
                    raise ValueError(f"Measured benchmark names differ from obligations for {attempt}")
                positive = {case.get("name") for case in ET.parse(reports[attempt, "test.xml"]).getroot().iter("testcase")
                            if not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))}
                for name, row in zip(names, rows):
                    leaf = next(leaf for leaf in leaves if leaf.name == name)
                    if (name not in positive or set(mapping(row["metrics"])) != set(leaf.units)
                            or leaf.iterations is not None and row["iterations"] != leaf.iterations):
                        raise ValueError(f"Benchmark measurement/XML differs from obligations for {attempt}: {name}")
            consumers.append({**asdict(attempt), "execution": execution_identity, "images": loaded, "cri": cri_evidence,
                              "image_build": image_build,
                              "test_log": {"path": log_path, **file_record(log)}})
        if observed != attempts:
            raise ValueError(f"Missing configured TestRunners: {sorted(attempts - observed)}")
        for rows in image_builds.values():
            image_command_partitions(rows)
    except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile) as error:
        errors.append(str(error))
    canonical_status = verdict.get("status", "INCOMPLETE")
    status = canonical_status if canonical_status in ("FAIL", "INCOMPLETE") else "INCOMPLETE" if errors else "PASS"
    return {"schema_version": 1, "status": status, "audit_status": "INCOMPLETE" if errors else "PASS",
            "canonical_status": canonical_status, "native_exit": verdict.get("native_exit"), "source": verdict.get("source"),
            "errors": errors, "images": images, "consumers": consumers,
            "inputs": inputs}


def audit_source_images(execution: Path, decoder: Path, canonical: Path, manifest: Path,
                        expectations: Path) -> dict[str, object]:
    """Decode the retained compact log with the caller's existing bb binary."""
    with tempfile.TemporaryDirectory(prefix="qualification-source-images-") as directory:
        raw = Path(directory) / "execution.json"
        try:
            inputs = [{"path": str(path), **file_record(path)} for path in (execution, decoder)]
            with raw.open("w") as stream:
                subprocess.run([str(decoder.resolve()), "print", f"--compact_execution_log={execution.resolve()}",
                                "--raw=true", "--sort=false"], stdout=stream, stderr=subprocess.PIPE,
                               text=True, check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            try:
                original = read_json(canonical)
            except (OSError, ValueError):
                original = {}
            diagnostic = str(error)
            if isinstance(error, subprocess.CalledProcessError) and error.stderr:
                diagnostic += "\n" + error.stderr
            return {"schema_version": 1, "status": "FAIL" if original.get("status") == "FAIL" else "INCOMPLETE",
                    "canonical_status": original.get("status"), "native_exit": original.get("native_exit"),
                    "source": original.get("source"), "audit_status": "INCOMPLETE", "errors": [diagnostic]}
        result = source_image_audit(raw, canonical, manifest, expectations)
        result["compact_execution"] = inputs
        by_path = {item["path"]: item for item in result["inputs"]}
        result["input_roles"] = {"execution": inputs[0], **{
            name: by_path[str(path)] for name, path in (
                ("verdict", canonical), ("manifest", manifest), ("expectations", expectations))
            if str(path) in by_path}}
        # Keep the decoded content identity, not the removed temporary path.
        for item in result["inputs"]:
            if item["path"] == str(raw):
                item["path"] = "decoded compact execution log"
        return result


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


def required_cases(path: Path, labels: set[str]) -> dict[str, frozenset[str]]:
    """Read the XML classname/name pairs that must pass for each selected owner."""
    raw = read_json(path)
    if not raw or raw.keys() - labels:
        raise ValueError(f"Required cases must name selected test owners: {sorted(raw.keys() - labels)}")
    owners = {}
    for label, values in raw.items():
        names = [string(value) for value in sequence(values)]
        if not names or any(not name.strip() for name in names) or len(set(names)) != len(names):
            raise ValueError(f"Expected distinct, nonempty required case names for {label}")
        owners[label] = frozenset(names)
    return owners


def preflight(events: Path, native_exit: int, kind: str, case_manifest: Path | None = None) -> dict[str, object]:
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
        if case_manifest is not None:
            if kind != "test":
                raise ValueError("Required cases apply only to test invocations")
            required_cases(case_manifest, set(targets))
        groups = declared_output_groups(targets)
        if groups:
            output_options = ["--output_groups=+" + ",+".join(sorted({group for _, group in groups})),
                              "--remote_download_outputs=toplevel"]
        if kind == "test":
            paths: dict[str, list[str]] = {}
            for label, target in sorted(targets.items()):
                pattern = result_output_pattern(target.tags)
                if not pattern:
                    continue
                repository, separator, local = label.partition("//")
                if not separator or ":" not in local:
                    raise ValueError(f"Invalid result owner label: {label}")
                path = local.replace(":", "/", 1).lstrip("/")
                if repository:
                    path = "external/" + repository.lstrip("@") + "/" + path
                paths.setdefault(pattern, []).append(re.escape(path))
            if paths:
                # Minimal remote downloads omit passing undeclared outputs.
                # Retain each owner's result files without downloading images
                # or successful debug logs.
                alternatives = [r"(?:" + "|".join(owners) + r")/(?:[^/]+/)*test\.outputs/(?:" + pattern + ")"
                                for pattern, owners in paths.items()]
                pattern = r".*/testlogs/(?:" + "|".join(alternatives) + ")$"
                output_options.extend(["--nozip_undeclared_test_outputs", "--remote_download_regex=" + pattern])
    except (OSError, ValueError) as error:
        errors.append(str(error))
    return {"schema_version": 1, "status": "INCOMPLETE" if errors else "PASS",
            "kind": kind, "native_exit": native_exit, "errors": errors,
            "output_options": output_options,
            "source_image_aliases": [asdict(Owner(label, target.configuration)) for label, target in sorted(targets.items())
                                     if target.is_alias and SOURCE_IMAGE_ASPECT in target.aspects],
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
            declared_benchmarks: Path | None = None, case_manifest: Path | None = None) -> dict[str, object]:
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
    retained, reports, report_errors = retained_reports(manifest)
    errors.extend(report_errors)
    if retained.get("events") != file_record(events):
        errors.append("Sanitized BEP digest/size differs from capture manifest")
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
    php_tests: set[Owner] = set()
    nodejs_tests: set[Owner] = set()
    exact_nodejs_tests: set[Owner] = set()
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
                    if "php-results" in details.get("tag", []):
                        php_tests.update(Owner.parse(child["targetCompleted"]) for child in children
                                         if "targetCompleted" in child and not mapping(child["targetCompleted"]).get("aspect"))
                    if NODE_RESULT_TAGS.intersection(details.get("tag", [])):
                        nodejs_tests.update(Owner.parse(child["targetCompleted"]) for child in children
                                           if "targetCompleted" in child and not mapping(child["targetCompleted"]).get("aspect"))
                    if "nodejs-exact-results" in details.get("tag", []):
                        exact_nodejs_tests.update(Owner.parse(child["targetCompleted"]) for child in children
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
                                if NODE_RESULT_TAGS.intersection(target.tags))
            exact_nodejs_tests.update(Owner(label, target.configuration) for label, target in targets.items()
                                      if "nodejs-exact-results" in target.tags)
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
    named_cases: dict[str, frozenset[str]] = {}
    if case_manifest is not None:
        try:
            if info["command_kind"] != "test":
                raise ValueError("Required cases apply only to ordinary test invocations")
            named_cases = required_cases(case_manifest, {owner.label for owner in expected})
        except (OSError, ValueError) as error:
            errors.append(f"Required cases: {error}")
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
    required_by_run: dict[tuple[Owner, int], dict[str, set[str]]] = {}
    unpassed_cases: list[dict[str, object]] = []
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
                                             ("nodejs", nodejs_tests, nodejs_results),
                                             ("php", php_tests, php_results)):
                if attempt.owner not in owners:
                    continue
                try:
                    if contract == "nodejs":
                        runtime = nodejs_results(reports[(attempt, "test.log")], attempt, reports,
                                                 exact_ids=attempt.owner in exact_nodejs_tests)
                    else:
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
                required = named_cases.get(attempt.owner.label, frozenset())
                counts = xml_cases(xml, required)
                if required:
                    # Shards divide a suite; another run or configuration
                    # cannot supply a case missing from this one.
                    observed = required_by_run.setdefault((attempt.owner, attempt.run), {})
                    for outcome, names in (("passed", counts["required_passed"]), ("failed", counts["failures"]),
                                           ("skipped", [mapping(item)["name"] for item in sequence(counts["skips"])])):
                        for name in required.intersection(sequence(names)):
                            observed.setdefault(name, set()).add(outcome)
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
        for (owner, run), observed in sorted(required_by_run.items()):
            missing.extend({**asdict(owner), "run": run, "name": name, "reason": "required case not reported"}
                           for name in sorted(named_cases[owner.label] - observed.keys()))
            unpassed_cases.extend({**asdict(owner), "run": run, "name": name, "outcomes": sorted(outcomes)}
                                  for name, outcomes in sorted(observed.items()) if "passed" not in outcomes)
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
    if (info["native_exit"] != 0 and not accepted_analysis_exit) or failures:
        status = "FAIL"
    elif errors or missing or unpassed_cases:
        status = "INCOMPLETE"
    else:
        status = "PASS"
    now = datetime.datetime.now(datetime.timezone.utc)
    return {"schema_version": 1, **info, "status": status, "accepted_analysis_exit": accepted_analysis_exit,
            "errors": errors, "failures": failures,
            "build_outputs": build_outputs,
            "expected_labels": sorted(labels or {owner.label for owner in expected}),
            "expected": [asdict(owner) for owner in sorted(expected)], "executed": observations,
            "expected_aspects": [{**asdict(aspect.owner), "aspect": aspect.name} for aspect in sorted(expected_aspects)],
            "completed_aspects": [{**asdict(aspect.owner), "aspect": aspect.name, "success": ok}
                                  for aspect, ok in sorted(completed_aspects.items())],
            "missing": missing, "skips": skips, "unpassed_required_cases": unpassed_cases, "case_counts": dict(cases),
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


def go_case_failure(directory: Path) -> dict[str, object]:
    """Authenticate an authoring abort without fabricating a native invocation."""
    record = read_json(directory / "preparation-failure.json")
    if record.keys() != {"source", "target_pattern_files"}:
        raise ValueError("Malformed Go case preparation failure")
    source = string(record.get("source"))
    arguments = (directory / "arguments.txt").read_text().splitlines()
    pattern_files = [argument.split("=", 1)[1] for argument in arguments
                     if argument.startswith("--target_pattern_file=")]
    if len(pattern_files) > 1 or record["target_pattern_files"] != pattern_files:
        raise ValueError("Preparation target pattern files differ from test arguments")
    labels = [argument for argument in arguments
              if argument.startswith("//") or argument.startswith("@") and "//" in argument]
    if pattern_files:
        labels.extend((directory / "target-patterns.txt").read_text().splitlines())
    if (not arguments or arguments[0] != "test" or not labels or len(labels) != len(set(labels))
            or any(not (label.startswith("//") or label.startswith("@") and "//" in label)
                   or any(c.isspace() for c in label) for label in labels)):
        raise ValueError("Preparation failure requires distinct requested target labels")
    paths = {name: directory / name for name in (
        "preparation-failure.json", "commit.txt", "source-before.txt", "source-after.txt",
        "final-head.txt", "arguments.txt", "go-case-options.txt", "go-case-exit.txt",
        "go-case-stdout.log", "go-case-stderr.log",
    )}
    paths["go-cases.json"] = directory.parent / "go-cases.json"
    paths["go-cases.sha256"] = directory.parent / "go-cases.sha256"
    if pattern_files:
        paths["target-patterns.txt"] = directory / "target-patterns.txt"
    checksum = paths["go-cases.sha256"].read_text().rstrip("\n").split("  ", 1)
    match checksum:
        case [digest, filename] if (re.fullmatch(r"[0-9a-f]{64}", digest)
                                   and Path(filename).name == "go-cases.json"):
            if hashlib.sha256(paths["go-cases.json"].read_bytes()).hexdigest() != digest:
                raise ValueError("Go case input differs from its pre-run checksum")
        case _:
            raise ValueError("Malformed pre-run Go case input checksum")
    if any((directory / name).exists() for name in ("native-exit.txt", "events.jsonl", "execution.binpb")):
        raise ValueError("Preparation failure unexpectedly contains native execution records")
    evidence = {name: {"path": str(path.relative_to(directory.parent)), **file_record(path)}
                for name, path in paths.items()}
    body = paths["commit.txt"].read_bytes()
    actual = hashlib.sha1(b"commit " + str(len(body)).encode() + b"\0" + body).hexdigest()
    if actual != source or paths["final-head.txt"].read_text().strip() != source:
        raise ValueError("Preparation source commit or final HEAD differs")
    if any(paths[name].read_text().strip() for name in ("source-before.txt", "source-after.txt")):
        raise ValueError("Preparation changed tracked source")
    exit_code = int(paths["go-case-exit.txt"].read_text())
    if not 1 <= exit_code <= 255:
        raise ValueError("Preparation failure requires a nonzero authoring exit")
    return {"source": source, "stage": "go-case-analysis", "exit": exit_code,
            "native_execution_started": False, "requested_labels": labels,
            "evidence": evidence}


def summarize_verdicts(rows: list[dict[str, object]], runner_exit: int, capture_exit: int,
                       errors: list[str], preparations: tuple[dict[str, object], ...] = ()) -> dict[str, object]:
    """Keep original command exits distinct from the enclosing runner's exit."""
    if not rows and not preparations:
        errors.append("No invocation verdicts were retained")
    sources = ({string(mapping(row["verdict"]).get("source")) for row in rows}
               | {string(record["source"]) for record in preparations})
    if len(sources) != 1:
        errors.append("Invocation sources are absent or inconsistent")
    for record in preparations:
        errors.append(f"Go case analysis exited {record['exit']}; native execution did not start")
    verdicts = [mapping(row["verdict"]) for row in rows]
    cases: Counter[str] = Counter()
    for row in verdicts:
        cases.update({key: number(value) for key, value in mapping(row.get("case_counts", {})).items()})
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
    return {"schema_version": 1, "status": status, "sources": sorted(sources), "native_exit": native_exit,
              "native_exits": native_exits, "runner_exit": runner_exit, "coordinator_failed": coordinator_failed,
              "capture_exit": capture_exit, "invocations": rows, "errors": errors,
              "expected": [{"invocation": row["path"], **mapping(item)} for row in rows for item in sequence(mapping(row["verdict"]).get("expected", []))],
              "executed": [{"invocation": row["path"], **mapping(item)} for row in rows for item in sequence(mapping(row["verdict"]).get("executed", []))],
              "commands": [{"invocation": row["path"], **{key: mapping(row["verdict"])[key] for key in (
                  "source", "status", "bazel_command", "arguments", "native_exit", "command_outputs", "execution_finished_at",
              )}} for row in rows if mapping(row["verdict"]).get("command_kind") == "command"],
              "case_counts": dict(cases),
              "zero_case_owners": [item for row in verdicts for item in sequence(row.get("zero_case_owners", []))],
              "non_runtime_owners": [item for row in verdicts for item in sequence(row.get("non_runtime_owners", []))],
              "all_skipped_owners": [item for row in verdicts for item in sequence(row.get("all_skipped_owners", []))],
              "failures": [item for row in verdicts for item in sequence(row.get("failures", []))],
              "build_outputs": [{"invocation": row["path"], **mapping(item)} for row in rows
                                for item in sequence(mapping(row["verdict"]).get("build_outputs", []))],
              "expected_failures": [item for row in verdicts for item in sequence(row.get("expected_failures", []))],
              "missing": ([item for row in verdicts for item in sequence(row.get("missing", []))]
                          + [{"label": label, "reason": "native execution did not start: Go case analysis failed"}
                             for record in preparations for label in sequence(record["requested_labels"])]),
              **({"preparation_failures": list(preparations),
                  "requested_labels": sorted({string(label) for record in preparations
                                              for label in sequence(record["requested_labels"])})}
                 if preparations else {}),
              "unpassed_required_cases": [{"invocation": row["path"], **mapping(item)} for row in rows
                                         for item in sequence(mapping(row["verdict"]).get("unpassed_required_cases", []))],
              "skips": [item for row in verdicts for item in sequence(row.get("skips", []))],
              "verdict_available_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def archive_evidence(directory: Path, output: Path) -> None:
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


def bundle(directory: Path, runner_exit: int, capture_exit: int, output: Path) -> dict[str, object]:
    rows = []
    errors = []
    preparations = []
    for invocation in sorted(directory.rglob("invocation-*")):
        if not invocation.is_dir():
            continue
        path = invocation / "verdict.json"
        try:
            if not path.exists() and (invocation / "preparation-failure.json").exists():
                preparations.append(go_case_failure(invocation))
            else:
                rows.append({"path": str(path.relative_to(directory)), "verdict": read_json(path), **file_record(path)})
        except (OSError, ValueError) as error:
            errors.append(str(error))
    result = summarize_verdicts(rows, runner_exit, capture_exit, errors, tuple(preparations))
    metadata = directory / "matrix-row.json"
    if metadata.exists():
        try:
            result["matrix_row"] = read_json(metadata)
            result["matrix_row_evidence"] = file_record(metadata)
        except (OSError, ValueError) as error:
            errors.append(f"Matrix row metadata: {error}")
            if result["status"] != "FAIL":
                result["status"] = "INCOMPLETE"
    lanes = directory / "lane-results.jsonl"
    if lanes.exists():
        try:
            result["lane_results"] = [mapping(json.loads(line)) for line in lanes.read_text().splitlines()]
            result["lane_results_evidence"] = file_record(lanes)
        except (OSError, ValueError) as error:
            errors.append(f"Lane completion metadata: {error}")
            if result["status"] != "FAIL":
                result["status"] = "INCOMPLETE"
    audits = directory / "matrix-audits.json"
    if audits.exists():
        try:
            result["matrix_audits"] = sequence(read_json(audits).get("audits"))
            result["matrix_audit_evidence"] = file_record(audits)
        except (OSError, ValueError) as error:
            errors.append(f"Matrix audit metadata: {error}")
            if result["status"] != "FAIL":
                result["status"] = "INCOMPLETE"
    write_json(directory / "verdict.json", result)
    archive_evidence(directory, output)
    return result


def bundle_members(artifact: Path, names: set[str], *, verify: dict[str, dict[str, object]] | None = None,
                   deadline: float | None = None, first: bool = False) -> dict[str, bytes]:
    """Retain small reports and stream-hash other inputs in one bounded pass."""
    verify = verify or {}
    wanted = names | verify.keys()
    if any(not name or name.startswith("/") or any(part in ("", ".", "..") for part in name.split("/")) for name in wanted):
        raise ValueError("Invalid bundle member path")
    result = {}
    seen = set()
    total = 0
    if not wanted:
        return result

    def retain(name: str, size: int, stream: BinaryIO) -> None:
        nonlocal total
        if name in seen or (name in names and size > MAX_REPORT_BYTES) or total + size > MAX_TOTAL_REPORT_BYTES:
            raise ValueError(f"Duplicate bundle member or report size bound: {name}")
        digest = hashlib.sha256()
        chunks = []
        length = 0
        reader = DeadlineReader(stream, deadline)
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(chunk)
            length += len(chunk)
            if name in names:
                chunks.append(chunk)
        if length != size:
            raise ValueError(f"Bundle member size differs: {name}")
        if name in verify and {"sha256": digest.hexdigest(), "bytes": length} != {
                key: verify[name].get(key) for key in ("sha256", "bytes")}:
            raise ValueError(f"Retained audit input differs from its digest/size: {name}")
        if name in names:
            result[name] = b"".join(chunks)
        seen.add(name)
        total += size
        check_deadline(deadline)

    def read_tar(stream: BinaryIO, selected: set[str]) -> None:
        with tarfile.open(fileobj=DeadlineReader(stream, deadline), mode="r|gz") as archive:
            for member in archive:
                if member.name not in selected:
                    continue
                if not member.isfile():
                    raise ValueError(f"Bundle member is not a regular file: {member.name}")
                with archive.extractfile(member) as source:
                    retain(member.name, member.size, source)
                if first and selected <= seen:
                    return

    if zipfile.is_zipfile(artifact):
        with zipfile.ZipFile(artifact) as archive:
            # Composition checks the nested original in full. Receipt retains
            # its established fast path to the separately uploaded verdict.
            if not first and "qualification.tar.gz" in archive.namelist():
                if archive.namelist().count("qualification.tar.gz") != 1:
                    raise ValueError("Duplicate nested qualification bundle")
                with archive.open("qualification.tar.gz") as stream:
                    read_tar(stream, wanted)
            else:
                direct = wanted & {name.removeprefix("qualification/") for name in archive.namelist()
                                   if name.startswith("qualification/")}
                for name in sorted(direct):
                    full_name = "qualification/" + name
                    member = archive.getinfo(full_name)
                    if archive.namelist().count(full_name) != 1 or member.is_dir():
                        raise ValueError(f"Duplicate or invalid bundle member: {name}")
                    with archive.open(member) as stream:
                        retain(name, member.file_size, stream)
                if remaining := wanted - seen:
                    with archive.open("qualification.tar.gz") as stream:
                        read_tar(stream, remaining)
    else:
        with artifact.open("rb") as stream:
            read_tar(stream, wanted)
    if missing := wanted - seen:
        raise ValueError(f"Bundle members are missing: {sorted(missing)}")
    return result


def bundle_member(artifact: Path, name: str, *, deadline: float | None = None) -> bytes:
    return bundle_members(artifact, {name}, deadline=deadline)[name]


def producer_verdict(artifact: Path, *, deadline: float | None = None) -> dict[str, object]:
    return mapping(json.loads(bundle_members(artifact, {"verdict.json"}, deadline=deadline, first=True)["verdict.json"]))


def receipt(artifact: Path, sha256: str, expected_source: str,
            targets: list[str] | None = None, *, deadline: float | None = None) -> dict[str, object]:
    """Authenticate a downloaded artifact and retain its producer verdict."""
    identity = file_record(artifact, deadline=deadline)
    if identity["sha256"] != sha256.removeprefix("sha256:"):
        raise ValueError("Artifact SHA256 differs from the expected digest")
    producer = producer_verdict(artifact, deadline=deadline)
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
        invocation["expected_owner_count"] = len(sequence(verdict.get("expected", [])))
        invocation["executed_attempt_count"] = len(sequence(verdict.get("executed", [])))
        for key in ("expected_aspects", "completed_aspects"):
            if key in verdict:
                invocation[key] = verdict[key]
        invocations.append(invocation)
    check_deadline(deadline)
    return {**result, "artifact": {"path": str(artifact), **identity},
            "expected_labels": labels, "expected_owner_count": len(expected),
            "executed_attempt_count": len(sequence(producer["executed"])), "invocations": invocations,
            "consumer_verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def source_image_writer(graph: dict[str, object], output: str, contents: bytes) -> Owner:
    """Identify the configured aspect action that wrote this exact output-group artifact."""
    def rows(name: str) -> dict[int, dict[str, object]]:
        items = [mapping(raw) for raw in sequence(graph.get(name))]
        return {number(item["id"]): item for item in items}

    fragments = rows("pathFragments")
    artifacts = rows("artifacts")
    targets = rows("targets")
    configurations = rows("configuration")
    aspects = rows("aspectDescriptors")
    writers = []
    for raw in sequence(graph.get("actions")):
        action = mapping(raw)
        if (action.get("mnemonic") != "FileWrite"
                or action.get("fileContents") != contents.decode()
                or not any(string(aspects[number(aspect_id)]["name"]).removeprefix("@@") == SOURCE_IMAGE_ASPECT
                           for aspect_id in sequence(action.get("aspectDescriptorIds", [])))):
            continue
        for output_id in sequence(action.get("outputIds")):
            id = number(artifacts[number(output_id)]["pathFragmentId"])
            parts = []
            while id:
                if len(parts) >= len(fragments):
                    raise ValueError("Cycle in Bazel output path fragments")
                fragment = fragments[id]
                parts.append(string(fragment["label"]))
                id = number(fragment.get("parentId", 0))
            if "/".join(reversed(parts)) == output:
                label = string(targets[number(action["targetId"])]["label"])
                writers.append(Owner(label.removeprefix("@@") if label.startswith("@@//") else label,
                                     string(configurations[number(action["configurationId"])]["checksum"])))
    if len(writers) != 1:
        raise ValueError(f"Expected one configured aspect writer for {output}, found {len(writers)}")
    return writers[0]


def source_image_expectations(canonical: Path, manifest: Path) -> dict[str, object]:
    """Read complete configured contracts, including empty and dynamic-builder roots."""
    verdict = read_json(canonical)
    expected_rows = sequence(verdict.get("expected"))
    expected = {Owner(string(mapping(raw).get("label")), string(mapping(raw).get("configuration")))
                for raw in expected_rows}
    if len(expected) != len(expected_rows):
        raise ValueError("Duplicate configured owners in canonical verdict")
    configured = {}
    owners = {}
    retained = {}
    for raw in sequence(verdict.get("build_outputs")):
        item = mapping(raw)
        if item.get("group") != "source_image_contracts":
            continue
        owner = Owner.parse(item.get("identity"))
        name = string(item.get("name"))
        key = owner, name
        if key in retained:
            raise ValueError(f"Duplicate source-image output: {key}")
        path = (manifest.parent / string(item.get("path"))).resolve()
        path.relative_to(manifest.parent.resolve())
        if file_record(path) != {"sha256": item.get("sha256"), "bytes": item.get("bytes")}:
            raise ValueError("Source-image contract differs from retained build output")
        retained[key] = path
    used = set()
    for (owner, name), path in retained.items():
        if not name.endswith(".source-images.json"):
            continue
        contract = read_json(path)
        if contract.get("schema_version") != 1 or owner in configured:
            raise ValueError(f"Wrong or duplicate configured source-image contract: {owner}")
        producer = None
        if contract.get("owner") != owner.label:
            # Bazel reports the alias in the aspect completion, while the generating
            # action belongs to its configured referent. Join them through the artifact.
            selection = read_json(canonical.parent / "selection.json")
            if asdict(owner) not in sequence(selection.get("source_image_aliases")):
                raise ValueError(f"Wrong configured source-image contract for {owner}")
            graph = canonical.parent / "source-image-writers.json"
            writer = source_image_writer(read_json(graph), name, path.read_bytes())
            if writer != Owner(string(contract.get("owner")), owner.configuration):
                raise ValueError(f"Configured source-image writer differs for {owner}: {writer}")
            producer = {**asdict(writer), "graph": {"path": str(graph), **file_record(graph)}}
        used.add((owner, name))
        images = declared_source_images(contract.get("images"))
        commands = [mapping(command) for command in sequence(contract.get("source_commands"))]
        inventory = list(images)
        for command in commands:
            fields = {"owner", "architecture", "image_class", "images"}
            if (command.keys() not in (fields, fields | {"contexts", "context_images"})
                    or not string(command["owner"]) or command["architecture"] not in ("x86_64", "aarch64")
                    or command["image_class"] not in ("cpu", "gpu")
                    or any(not string(image) for image in sequence(command["images"]))):
                raise ValueError(f"Invalid declared image-source command: {command}")
            if "contexts" in command:
                if not string(command["contexts"]):
                    raise ValueError("Image-source command requires its configured contexts archive")
                inventory.extend(declared_source_images(command["context_images"]))
        pins = {}
        indexes = {}
        for image in inventory:
            if image.get("origin") != "registry":
                continue
            pin_name = string(image["pin"])
            key = owner, pin_name
            if key not in retained or not re.search(r"\.source-image-pin-[0-9]+$", pin_name):
                raise ValueError(f"Missing configured registry pin output: {key}")
            pin_path = retained[key]
            digest = pin_path.read_text().strip()
            if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                raise ValueError(f"Invalid registry pin bytes: {pin_name}")
            used.add(key)
            pins[pin_name] = {"digest": digest, "evidence": {"path": str(pin_path), **file_record(pin_path)}}
            index_name = string(image["index"])
            key = owner, index_name
            if key not in retained or not re.search(r"\.source-image-index-[0-9]+\.json$", index_name):
                raise ValueError(f"Missing configured registry index output: {key}")
            index_path = retained[key]
            body = index_path.read_text()
            mapping(json.loads(body))
            used.add(key)
            indexes[index_name] = {"body": body, "evidence": {"path": str(index_path), **file_record(index_path)}}
        names = sorted({string(image["name"]) for image in images})
        owners[owner.label] = sorted(set(owners.get(owner.label, [])) | set(names))
        configured[owner] = {**asdict(owner), "images": images, "source_commands": commands, "pins": pins, "indexes": indexes,
                             "contract": {"path": str(path), **file_record(path)}}
        if producer is not None:
            configured[owner]["producer"] = producer
    if not expected or configured.keys() != expected or used != retained.keys():
        raise ValueError("Source-image contracts differ from complete configured owners or pin outputs")
    return {"owners": owners, "configured_owners": [configured[owner] for owner in sorted(configured)]}


def source_image_row(directory: Path, decoder: Path) -> dict[str, object]:
    """Audit every execution phase before the row's existing bundle step."""
    metadata = read_json(directory / "matrix-row.json")
    row = mapping(metadata.get("row"))
    required = sequence(row.get("required_audits"))
    audits = []
    if required:
        if required != ["source-images"]:
            raise ValueError(f"Unsupported required row audits: {required}")
        audit = {"id": "source-images", **{key: metadata[key] for key in ("source", "run_id", "run_attempt")},
                 "row_id": row["id"], "status": "PASS", "invocations": [], "errors": []}
        for invocation in sorted(directory.glob("invocation-*")):
            if not invocation.is_dir():
                continue
            canonical = invocation / "verdict.json"
            try:
                value = read_json(canonical)
                if value.get("command_kind") not in ("test", "benchmark", "build"):
                    continue
                if value.get("source") != metadata["source"]:
                    raise ValueError("Invocation source differs from row metadata")
                entry = {"path": str(canonical.relative_to(directory)), "inputs": {}}
                audit["invocations"].append(entry)
                manifest = manifest_path(invocation / "events.jsonl")
                expectations = invocation / "source-image-expectations.json"
                write_json(expectations, source_image_expectations(canonical, manifest))
                execution = invocation / "execution.binpb"
                entry["inputs"] = {name: {"path": str(path.relative_to(directory)), **file_record(path)}
                                   for name, path in (("expectations", expectations), ("execution", execution),
                                                      ("verdict", canonical), ("manifest", manifest))}
                evidence = invocation / "source-image-audit.json"
                result = audit_source_images(execution, decoder, canonical, manifest, expectations)
                if any("producer" in owner for owner in read_json(expectations)["configured_owners"]):
                    graph = invocation / "source-image-writers.json"
                    entry["inputs"]["writers"] = {"path": str(graph.relative_to(directory)), **file_record(graph)}
                    result.setdefault("input_roles", {})["writers"] = {"path": str(graph), **file_record(graph)}
                write_json(evidence, result)
                entry["evidence"] = {"path": str(evidence.relative_to(directory)), **file_record(evidence)}
                if result.get("audit_status") != "PASS":
                    raise ValueError(f"Source-image audit incomplete: {canonical}")
            except (OSError, ValueError, KeyError) as error:
                audit["status"] = "INCOMPLETE"
                audit["errors"].append(str(error))
        if not audit["invocations"]:
            audit["status"] = "INCOMPLETE"
            audit["errors"].append("No execution phases were retained for the required source-image audit")
        audits.append(audit)
    result = {"schema_version": 1, "audits": audits}
    write_json(directory / "matrix-audits.json", result)
    return result


def matrix_plan(value: dict[str, object]) -> dict[str, dict[str, object]]:
    """Validate composition identity; lane and resource policy belong to the planner."""
    if value.get("schema_version") != 1 or re.fullmatch(r"[0-9a-f]{40}", string(value.get("source"))) is None:
        raise ValueError("Expected a versioned matrix plan with a full source SHA")
    if re.fullmatch(r"[1-9][0-9]*", string(value.get("run_id"))) is None:
        raise ValueError("Matrix run_id must be a canonical positive decimal string")
    if type(value.get("run_attempt")) is not int or value["run_attempt"] <= 0:
        raise ValueError("Matrix run_attempt must be a positive integer")
    mapping(value.get("side_inputs"))
    rows = {}
    for raw in sequence(value.get("rows")):
        row = mapping(raw)
        name = string(row.get("id"))
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) is None or name in rows:
            raise ValueError(f"Invalid or duplicate matrix row: {name}")
        for key in ("lanes", "required_kinds", "required_audits"):
            values = [string(item) for item in sequence(row.get(key))]
            if len(values) != len(set(values)) or any(not item for item in values):
                raise ValueError(f"Duplicate or empty {key} for row {name}")
        if not sequence(row["lanes"]):
            raise ValueError(f"Matrix row has no lanes: {name}")
        kinds = set(sequence(row["required_kinds"]))
        if not kinds or not kinds <= {"test", "benchmark", "build", "analysis", "command"}:
            raise ValueError(f"Invalid required command kinds for row {name}")
        rows[name] = row
    if not rows:
        raise ValueError("Matrix plan has no required rows")
    return rows


def audit_member(members: dict[str, bytes], raw: object) -> dict[str, object]:
    descriptor = mapping(raw)
    body = members[string(descriptor.get("path"))]
    if (hashlib.sha256(body).hexdigest(), len(body)) != (descriptor.get("sha256"), descriptor.get("bytes")):
        raise ValueError(f"Audit member digest/size differs: {descriptor.get('path')}")
    return mapping(json.loads(body))


def matrix_audits(members: dict[str, bytes], producer: dict[str, object], identity: dict[str, object],
                  row: dict[str, object]) -> list[dict[str, object]]:
    """Require complete per-invocation audits without replacing workload status."""
    wanted = set(sequence(row["required_audits"]))
    audits = [mapping(raw) for raw in sequence(producer.get("matrix_audits", []))]
    if len(audits) != len({string(audit.get("id")) for audit in audits}) or {audit.get("id") for audit in audits} != wanted:
        raise ValueError("Required audit IDs are missing, duplicated or unexpected")
    phases = {string(mapping(raw).get("path")): mapping(raw) for raw in sequence(producer["invocations"])
              if mapping(mapping(raw).get("verdict")).get("command_kind") in ("test", "benchmark", "build")}
    for audit in audits:
        if audit["id"] != "source-images":
            raise ValueError(f"Unsupported required audit: {audit['id']}")
        if any(audit.get(key) != value for key, value in identity.items()) or audit.get("row_id") != row["id"]:
            raise ValueError("Audit source/run/row identity differs")
        entries = [mapping(raw) for raw in sequence(audit.get("invocations"))]
        if (not phases or len(entries) != len({string(entry.get("path")) for entry in entries})
                or {entry.get("path") for entry in entries} != phases.keys()):
            raise ValueError("Audit invocation coverage differs from the row's execution phases")
        for entry in entries:
            phase = phases[string(entry["path"])]
            inputs = mapping(entry.get("inputs"))
            expectations = audit_member(members, inputs.get("expectations"))
            producers = [mapping(owner["producer"]) for raw in sequence(expectations.get("configured_owners", []))
                         if "producer" in (owner := mapping(raw))]
            required = {"expectations", "execution", "verdict", "manifest"}
            if producers:
                required.add("writers")
                writers = mapping(inputs.get("writers"))
                for producer in producers:
                    graph = mapping(producer.get("graph"))
                    if any(graph.get(key) != writers.get(key) for key in ("sha256", "bytes")):
                        raise ValueError("Configured producer graph differs from its retained input")
            if inputs.keys() != required:
                raise ValueError("Missing or unexpected source-image audit input")
            canonical = mapping(inputs["verdict"])
            if canonical.get("path") != entry["path"] or any(canonical.get(key) != phase.get(key) for key in ("sha256", "bytes")):
                raise ValueError("Audit canonical verdict differs from its authenticated invocation")
            value = audit_member(members, entry.get("evidence"))
            if value.get("source") != identity["source"] or value.get("canonical_status") != mapping(phase["verdict"])["status"]:
                raise ValueError("Audit source or canonical status differs")
            retained_inputs = mapping(value.get("input_roles"))
            for name, raw in inputs.items():
                descriptor = mapping(raw)
                retained = mapping(retained_inputs.get(name))
                relative = Path(string(descriptor.get("path"))).parts
                actual = Path(string(retained.get("path"))).parts
                if (not relative or actual[-len(relative):] != relative
                        or any(retained.get(key) != descriptor.get(key) for key in ("sha256", "bytes"))):
                    raise ValueError(f"Audit does not bind its declared {name} input role/path")
            outputs = [mapping(raw) for raw in sequence(mapping(phase["verdict"]).get("build_outputs", []))
                       if mapping(raw).get("group") == "source_image_contracts"]
            referenced = set()
            for raw_owner in sequence(expectations.get("configured_owners", [])):
                owner = mapping(raw_owner)
                descriptors = [mapping(owner.get("contract"))]
                descriptors.extend(mapping(mapping(raw).get("evidence")) for kind in ("pins", "indexes")
                                   for raw in mapping(owner.get(kind, {})).values())
                for descriptor in descriptors:
                    match [output for output in outputs if (
                            Owner.parse(output.get("identity")) == Owner(string(owner["label"]), string(owner["configuration"]))
                            and Path(string(descriptor.get("path"))).parts[-len(Path(string(output["path"])).parts):]
                            == Path(string(output["path"])).parts
                            and all(descriptor.get(key) == output.get(key) for key in ("sha256", "bytes")))]:
                        case [output]:
                            referenced.add((Owner.parse(output["identity"]), string(output["path"])))
                        case _:
                            raise ValueError("Configured contract/pin/index evidence differs from retained outputs")
            if referenced != {(Owner.parse(output["identity"]), string(output["path"])) for output in outputs}:
                raise ValueError("Source expectation evidence does not cover its configured outputs")
            if value.get("audit_status") != "PASS":
                raise ValueError(f"Required source-image audit is {value.get('audit_status', 'missing')}: {entry['path']}")
        if audit.get("status") != "PASS":
            raise ValueError(f"Required audit is {audit.get('status', 'missing')}: {audit['id']}")
    return audits


def compose(plan_path: Path, results_path: Path, directory: Path, output: Path) -> dict[str, object]:
    """Compose exactly the planned rows from authenticated, unchanged child bundles."""
    errors: list[str] = []
    invocations: list[dict[str, object]] = []
    observations = []
    plan: dict[str, object] = {}
    results: dict[str, object] = {}
    failed = False
    required: dict[str, dict[str, object]] = {}
    observed: set[str] = set()
    try:
        plan = read_json(plan_path)
        required = matrix_plan(plan)
        plan_identity = file_record(plan_path)
        identity = {key: plan[key] for key in ("source", "run_id", "run_attempt")}
        results = read_json(results_path)
        failed = results.get("matrix_result") in ("failure", "cancelled")
        if results.get("matrix_result") not in ("success", "failure", "cancelled", "skipped"):
            errors.append("Missing or invalid matrix job result")
        if results.get("matrix_result") == "skipped":
            errors.append("Matrix jobs were skipped")
        if results.get("transfer_result") != "success":
            errors.append("Artifact transfer did not complete successfully")
        if (results.get("schema_version") != 1 or results.get("plan_sha256") != plan_identity["sha256"]
                or any(results.get(key) != value for key, value in identity.items())):
            raise ValueError("Result manifest identifies a different plan/source/run/attempt")
        for raw in sequence(results.get("rows")):
            record = mapping(raw)
            name = string(record.get("id"))
            observation: dict[str, object] = {"id": name, "status": "INCOMPLETE"}
            observations.append(observation)
            try:
                if name not in required or name in observed:
                    raise ValueError(f"Unexpected or duplicate result row: {name}")
                observed.add(name)
                descriptor = mapping(record.get("artifact"))
                artifact = (results_path.parent / string(descriptor.get("path"))).resolve()
                artifact.relative_to(results_path.parent.resolve())
                child = receipt(artifact, string(descriptor.get("sha256")), string(plan["source"]))
                observation["receipt"] = child
                observation["status"] = child["status"]
                failed |= child["status"] == "FAIL"
                if mapping(child["artifact"])["bytes"] != descriptor.get("bytes"):
                    raise ValueError("Row artifact size differs")
                producer = producer_verdict(artifact)
                metadata = mapping(producer.get("matrix_row"))
                expected_metadata = {"schema_version": 1, "plan_sha256": plan_identity["sha256"],
                                     **identity, "side_inputs": plan["side_inputs"], "row": required[name]}
                if metadata != expected_metadata:
                    raise ValueError("Authenticated row metadata differs from its plan")
                child_path = directory / "rows" / name / ("qualification.zip" if zipfile.is_zipfile(artifact) else "qualification.tar.gz")
                child_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(artifact, child_path)
                retained_artifact = {"path": str(child_path.relative_to(directory)), **file_record(child_path)}
                if any(retained_artifact[key] != mapping(child["artifact"])[key] for key in ("sha256", "bytes")):
                    raise ValueError("Child artifact changed while retaining it")
                observation["artifact"] = retained_artifact
                phases = [mapping(item) for item in sequence(producer["invocations"])]
                if len(phases) != len({string(phase.get("path")) for phase in phases}):
                    raise ValueError("Duplicate invocation paths in row")
                for phase in phases:
                    if mapping(phase["verdict"]).get("source") != plan["source"]:
                        raise ValueError("Invocation source differs from the plan")
                    invocations.append({**phase, "artifact": retained_artifact,
                                        "path": f"rows/{name}/" + string(phase["path"])})
                member_names = {"verdict.json"} | {string(phase.get("path")) for phase in phases}
                inputs = {}
                for phase in phases:
                    for raw_output in sequence(mapping(phase["verdict"]).get("build_outputs", [])):
                        source_output = mapping(raw_output)
                        if source_output.get("group") == "source_image_contracts":
                            path = str(Path(string(phase["path"])).parent / string(source_output.get("path")))
                            descriptor = {"path": path, "sha256": source_output.get("sha256"), "bytes": source_output.get("bytes")}
                            if path in inputs and inputs[path] != descriptor:
                                raise ValueError(f"Conflicting source-contract output descriptors: {path}")
                            inputs[path] = descriptor
                for audit in sequence(producer.get("matrix_audits", [])):
                    for raw_entry in sequence(mapping(audit).get("invocations", [])):
                        entry = mapping(raw_entry)
                        member_names.add(string(mapping(entry.get("evidence")).get("path")))
                        member_names.add(string(mapping(mapping(entry.get("inputs")).get("expectations")).get("path")))
                        for raw_input in mapping(entry.get("inputs")).values():
                            descriptor = mapping(raw_input)
                            path = string(descriptor.get("path"))
                            if path in inputs and inputs[path] != descriptor:
                                raise ValueError(f"Conflicting audit input descriptors: {path}")
                            inputs[path] = descriptor
                members = bundle_members(artifact, member_names, verify=inputs)
                if mapping(json.loads(members["verdict.json"])) != producer:
                    raise ValueError("Nested producer verdict differs from its receipt")
                for phase in phases:
                    phase_body = members[string(phase.get("path"))]
                    if (hashlib.sha256(phase_body).hexdigest(), len(phase_body)) != (phase.get("sha256"), phase.get("bytes")):
                        raise ValueError("Invocation verdict differs from its retained digest/size")
                    if mapping(json.loads(phase_body)) != phase["verdict"]:
                        raise ValueError("Embedded invocation verdict differs from its retained member")
                completed_lanes = []
                for raw in sequence(producer.get("lane_results", [])):
                    lane = mapping(raw)
                    if type(lane.get("native_exit")) is not int or lane["native_exit"] < 0:
                        raise ValueError("Invalid lane native exit")
                    failed |= lane["native_exit"] != 0
                    names = [string(name) for name in sequence(lane.get("lanes"))]
                    if not names:
                        raise ValueError("Empty completed lane group")
                    completed_lanes.extend(names)
                if (len(completed_lanes) != len(set(completed_lanes))
                        or set(completed_lanes) != set(sequence(required[name]["lanes"]))):
                    raise ValueError("Completed lanes differ from the exact planned lane set")
                kinds = {mapping(phase["verdict"]).get("command_kind") for phase in phases}
                if missing_kinds := set(sequence(required[name]["required_kinds"])) - kinds:
                    raise ValueError(f"Missing required command kinds: {sorted(missing_kinds)}")
                for phase in phases:
                    value = mapping(phase["verdict"])
                    if value.get("command_kind") in ("test", "benchmark") and (not sequence(value.get("expected")) or not sequence(value.get("executed"))):
                        raise ValueError("Test/benchmark phase has no configured owners or executed attempts")
                if child["status"] != "PASS":
                    errors.append(f"Row {name} retains {child['status']}")
                observation["audits"] = matrix_audits(members, producer, identity, required[name])
            except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile, tarfile.TarError) as error:
                observation["error"] = str(error)
                errors.append(f"Row {name}: {error}")
        if missing := required.keys() - observed:
            errors.append(f"Missing result rows: {sorted(missing)}")
    except (OSError, ValueError, KeyError) as error:
        errors.append(str(error))
    try:
        result = summarize_verdicts(invocations, 0, 0, errors)
    except (ValueError, KeyError) as error:
        errors.append(f"Malformed invocation accounting: {error}")
        result = summarize_verdicts([], 0, 0, errors)
    if failed:
        result["status"] = "FAIL"
    elif errors and result["status"] != "FAIL":
        result["status"] = "INCOMPLETE"
    # Even an entirely missing matrix retains the requested source, never
    # invented invocation or coverage records.
    result["sources"] = [plan["source"]] if re.fullmatch(r"[0-9a-f]{40}", str(plan.get("source", ""))) else []
    result["matrix"] = {"plan": {"path": "plan.json", **file_record(plan_path)},
                        "results": {"path": "results.json", **file_record(results_path)},
                        "required_rows": sorted(required), "rows": observations,
                        "matrix_result": results.get("matrix_result"), "transfer_result": results.get("transfer_result"),
                        "artifact_verification": "Received child bytes are bound to the digest supplied by the transport; transport verification remains separate."}
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(plan_path, directory / "plan.json")
    shutil.copyfile(results_path, directory / "results.json")
    write_json(directory / "verdict.json", result)
    archive_evidence(directory, output)
    return result


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
            except OSError as error:
                if isinstance(error, (TimeoutError, ConnectionResetError)):
                    reason = type(error).__name__
                elif error.errno == errno.EADDRNOTAVAIL:
                    reason = "EADDRNOTAVAIL"
                else:
                    raise
                # A failed status read says nothing about the running build.
                # Retry that read within the original collection deadline.
                remaining()
                observation_retries.append({
                    "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "error": reason,
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
    copy.add_argument("--expected-kind", choices=("test", "build"), default="build")
    analyze = commands.add_parser("preflight")
    for name in ("events", "output"):
        analyze.add_argument("--" + name, type=Path, required=True)
    analyze.add_argument("--native-exit", type=int, required=True)
    analyze.add_argument("--kind", choices=("test", "build"), required=True)
    analyze.add_argument("--output-options", type=Path)
    analyze.add_argument("--required-cases", type=Path)
    check = commands.add_parser("verdict")
    for name in ("events", "context", "output"):
        check.add_argument("--" + name, type=Path, required=True)
    check.add_argument("--manifest", type=Path)
    check.add_argument("--expected-targets", type=Path)
    check.add_argument("--expected-analysis", type=Path, action="append", default=[])
    check.add_argument("--expected-benchmarks", type=Path)
    check.add_argument("--declared-benchmarks", type=Path)
    check.add_argument("--required-cases", type=Path)
    images = commands.add_parser("source-images", help="Audit declared source/archive/load obligations in one authenticated invocation")
    for name in ("execution-log", "decoder", "verdict", "manifest", "expectations", "output"):
        images.add_argument("--" + name, type=Path, required=True)
    image_contracts = commands.add_parser("source-image-expectations", help="Read source-owned configured image contracts")
    for name in ("verdict", "manifest", "output"):
        image_contracts.add_argument("--" + name, type=Path, required=True)
    row_audit = commands.add_parser("source-image-row", help="Audit all declared execution phases before bundling a matrix row")
    for name in ("directory", "decoder"):
        row_audit.add_argument("--" + name, type=Path, required=True)
    matrix = commands.add_parser("compose", help="Compose exactly the planned qualification rows")
    for name in ("plan", "results", "directory", "output"):
        matrix.add_argument("--" + name, type=Path, required=True)
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
    if args.command == "source-image-expectations":
        try:
            write_json(args.output, source_image_expectations(args.verdict, args.manifest))
        except (OSError, ValueError, KeyError) as error:
            parser.error(str(error))
        return 0
    if args.command == "source-image-row":
        try:
            result = source_image_row(args.directory, args.decoder)
        except (OSError, ValueError, KeyError) as error:
            parser.error(str(error))
        return int(any(mapping(audit).get("status") != "PASS" for audit in sequence(result["audits"])))
    if args.command == "compose":
        try:
            result = compose(args.plan, args.results, args.directory, args.output)
        except (OSError, ValueError, KeyError) as error:
            parser.error(str(error))
        print(f"{result['status']}: {args.output}")
        return int(result["status"] != "PASS")
    if args.command == "source-images":
        result = audit_source_images(args.execution_log, args.decoder, args.verdict, args.manifest, args.expectations)
        write_json(args.output, result)
        return 0 if result["status"] == "PASS" else number(result.get("native_exit") or 1)
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
                         tuple(args.expected_analysis), args.expected_kind)
        return int(bool(result["errors"]))
    if args.command == "preflight":
        result = preflight(args.events, args.native_exit, args.kind, args.required_cases)
        write_json(args.output, result)
        if args.output_options is not None and result["status"] == "PASS":
            args.output_options.write_text("".join(option + "\n" for option in result["output_options"]))
        return int(result["status"] != "PASS")
    if args.command == "verdict":
        result = verdict(args.events, args.manifest or manifest_path(args.events), args.context,
                         args.expected_targets, tuple(args.expected_analysis), args.expected_benchmarks,
                         args.declared_benchmarks, args.required_cases)
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
