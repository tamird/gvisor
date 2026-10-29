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
reads those original records; it does not query services or rerun workloads.
Context paths are relative to the context JSON, or absolute. The context names
expected_source, commit_file, source_before, source_after, final_head,
executed_arguments, native_exit, capture_exit, execution_finished_at (UTC), and
command_kind (test, build or analysis). Optional lock_before/lock_after must be
supplied together. Exit values are read from files, not inferred from reports.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile
import time
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

import unit_matrix


MAX_REPORT_BYTES = 512 * 1024 * 1024
MAX_TOTAL_REPORT_BYTES = 2 * 1024 * 1024 * 1024
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


def file_record(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def manifest_path(events: Path) -> Path:
    return Path(str(events) + ".manifest.json")


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


def capture(events: Path, output: Path, output_root: Path, report_dir: Path) -> dict[str, object]:
    """Copy all advertised original reports before returning capture failures."""
    errors: list[str] = []
    reports: list[dict[str, object]] = []
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
        with events.open() as source, output.open("w") as target:
            for line_number, line in enumerate(source, 1):
                try:
                    event = mapping(json.loads(line))
                    identity = mapping(event.get("id"))
                    if "testResult" in event:
                        attempt = Attempt.parse(identity.get("testResult"))
                        advertised = sequence(mapping(event["testResult"]).get("testActionOutput", []))
                        for name in ("test.log", "test.xml"):
                            try:
                                matches = [mapping(item) for item in advertised if mapping(item).get("name") == name]
                                if len(matches) != 1 or (attempt, name) in seen:
                                    raise ValueError(f"Expected one original {name} for {attempt}")
                                seen.add((attempt, name))
                                (report,) = matches
                                uri = urlsplit(string(report.get("uri")))
                                if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
                                    raise ValueError(f"Expected a local report URI for {attempt}: {name}")
                                path = Path(unquote(uri.path)).resolve()
                                relative = path.relative_to(output_root)
                                destination = (report_dir / relative).resolve()
                                destination.relative_to(report_dir)
                                old = previous.get((attempt, name))
                                if old is not None and old.get("path") == str(destination.relative_to(output.parent.resolve())) and destination.is_file():
                                    if file_record(destination) == {"sha256": old.get("sha256"), "bytes": old.get("bytes")}:
                                        total += number(old["bytes"])
                                        if total > MAX_TOTAL_REPORT_BYTES:
                                            raise ValueError("Retained reports exceed total size bound")
                                        reports.append(old)
                                        continue
                                size = path.stat().st_size
                                if not path.is_file() or size > MAX_REPORT_BYTES or total + size > MAX_TOTAL_REPORT_BYTES:
                                    raise ValueError(f"Report size/type exceeds retention bounds: {path}")
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                with path.open("rb") as src, destination.open("wb") as dst:
                                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                                record = file_record(destination)
                                if record["bytes"] != size:
                                    raise ValueError(f"Report changed during capture: {path}")
                                total += size
                                reports.append({"identity": identity["testResult"], "name": name,
                                                "path": str(destination.relative_to(output.parent.resolve())), **record})
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
    result = {"schema_version": 1, "input_events": input_events, "events": file_record(output), "reports": reports, "errors": errors}
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
    arguments = bodies.get("executed_arguments", b"").decode().splitlines()
    analysis = any(arg in ("--nobuild", "--nobuild=true", "--build=false") for arg in arguments)
    if kind not in ("test", "build", "analysis") or (kind == "analysis") != analysis:
        errors.append("Command kind does not match original analysis/execution arguments")
    command = next((arg for arg in arguments if not arg.startswith("-")), None)
    if command not in ("test", "build") or kind in ("test", "build") and command != kind:
        errors.append("Missing or inconsistent original Bazel command")
    exits = {}
    for key in ("native_exit", "capture_exit"):
        try:
            exits[key] = int(bodies[key].decode().strip())
        except (KeyError, ValueError):
            errors.append(f"Missing or invalid {key}")
            exits[key] = 1
    finished = datetime.datetime.fromisoformat(string(context.get("execution_finished_at")))
    if finished.utcoffset() != datetime.timedelta(0):
        raise ValueError("execution_finished_at must be UTC")
    return {"source": source, "source_evidence": evidence, "command_kind": kind,
            "arguments": arguments, "execution_finished_at": finished.isoformat(), **exits}, errors


def verdict(events: Path, manifest: Path, context: Path, expected_targets: Path | None = None,
            expected_analysis: tuple[Path, ...] = ()) -> dict[str, object]:
    """Reconcile configured owners and announced runs/shards with originals."""
    started = time.monotonic()
    errors: list[str] = []
    info, source_errors = source_context(context)
    errors.extend(source_errors)
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
    results: dict[Attempt, dict[str, object]] = {}
    summaries: dict[Owner, dict[str, object]] = {}
    finished: dict[str, object] = {}
    skipped_owners: set[Owner] = set()
    for line_number, line in enumerate(events.open(), 1):
        try:
            event = mapping(json.loads(line))
            identity = mapping(event.get("id"))
            children = [mapping(child) for child in sequence(event.get("children", []))]
            if "configured" in event:
                details = mapping(event["configured"])
                if details.get("targetKind") != "test_suite rule" and (info["command_kind"] != "test" or "testSize" in details):
                    configured.update(Owner.parse(child["targetCompleted"]) for child in children if "targetCompleted" in child)
            if "aborted" in event and mapping(event["aborted"]).get("reason") == "SKIPPED" and "targetCompleted" in identity:
                skipped_owners.add(Owner.parse(identity["targetCompleted"]))
            announced.update(Attempt.parse(child["testResult"]) for child in children if "testResult" in child)
            if "completed" in event:
                completed[Owner.parse(identity.get("targetCompleted"))] = mapping(event["completed"]).get("success") is True
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
    for analysis in expected_analysis:
        expected.update(Owner(label, target.configuration) for label, target in unit_matrix.configured_tests(str(analysis)).items())
    if labels and expected and labels != {owner.label for owner in expected}:
        errors.append("Expected labels differ from selected analysis leaves")
    if not expected:
        expected = {owner for owner in configured if not labels or owner.label in labels}
    if labels - {owner.label for owner in expected}:
        errors.append("Expected labels absent from configured invocation")
    if not expected:
        errors.append("No expected configured owners")
    if configured != expected:
        errors.append(f"Configured owners differ from expected: {sorted(configured ^ expected)}")
    if not finished:
        errors.append("Missing invocation finish event")
    if info["native_exit"] == 0 and finished.get("overallSuccess") is not True:
        errors.append("Native success disagrees with BEP finish")
    missing = [asdict(owner) for owner in sorted(expected - completed.keys())]
    missing.extend({"label": label, "configuration": None, "reason": "expected owner not configured"}
                   for label in sorted(labels - {owner.label for owner in expected}))
    failures: list[dict[str, object]] = [asdict(owner) | {"reason": "target build failed"} for owner, ok in sorted(completed.items()) if not ok]
    cases: Counter[str] = Counter()
    observations: list[dict[str, object]] = []
    owner_cases: Counter[Owner] = Counter()
    owner_executed: Counter[Owner] = Counter()
    skips: list[dict[str, object]] = []
    if info["command_kind"] == "test":
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
                    failures.append({**asdict(attempt), "reason": "XML test failures", "cases": counts["failures"]})
            except (OSError, ValueError, KeyError, ET.ParseError) as error:
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
        if not results or cases["passed"] + cases["failed"] == 0:
            errors.append("Test invocation has no executed named cases; zero-case/all-skipped success is not coverage")
    elif results or summaries:
        errors.append("Build/analysis invocation unexpectedly contains test execution")
    if info["capture_exit"] != 0:
        errors.append("Caller recorded a capture failure")
    status = "FAIL" if info["native_exit"] != 0 or failures else "INCOMPLETE" if errors or missing else "PASS"
    now = datetime.datetime.now(datetime.timezone.utc)
    return {"schema_version": 1, **info, "status": status, "errors": errors, "failures": failures,
            "expected_labels": sorted(labels or {owner.label for owner in expected}),
            "expected": [asdict(owner) for owner in sorted(expected)], "executed": observations,
            "missing": missing, "skips": skips, "case_counts": dict(cases),
            "zero_case_owners": [asdict(owner) for owner, count in sorted(owner_cases.items()) if count == 0],
            "all_skipped_owners": [asdict(owner) for owner, count in sorted(owner_executed.items()) if count == 0 and owner_cases[owner] > 0],
            "skipped_owners": [asdict(owner) for owner in sorted(skipped_owners)],
            "bep_finish": finished, "verdict_available_at": now.isoformat(),
            "reporting_elapsed_seconds": time.monotonic() - started,
            "execution_to_verdict_seconds": (now - datetime.datetime.fromisoformat(string(info["execution_finished_at"]))).total_seconds()}


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
            native_exits.append({"invocation": row["path"], "exit": number(mapping(row["verdict"]).get("native_exit"))})
        except ValueError as error:
            errors.append(str(error))
    native_exit = next((number(row["exit"]) for row in native_exits if row["exit"] != 0), 0) if native_exits else None
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
              "case_counts": dict(sum((Counter({key: number(value) for key, value in mapping(row.get("case_counts", {})).items()}) for row in verdicts), Counter())),
              "zero_case_owners": [item for row in verdicts for item in sequence(row.get("zero_case_owners", []))],
              "all_skipped_owners": [item for row in verdicts for item in sequence(row.get("all_skipped_owners", []))],
              "failures": [item for row in verdicts for item in sequence(row.get("failures", []))],
              "missing": [item for row in verdicts for item in sequence(row.get("missing", []))],
              "skips": [item for row in verdicts for item in sequence(row.get("skips", []))],
              "verdict_available_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    write_json(directory / "verdict.json", result)
    # The archive must be outside its input directory, and symlinks cannot
    # introduce private files from outside the owned artifact directory.
    if output.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Bundle output must be outside the evidence directory")
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Symlink in evidence directory: {path}")
            if path.is_file():
                archive.add(path, arcname=str(path.relative_to(directory)), recursive=False)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    copy = commands.add_parser("capture")
    for name in ("events", "output", "output-root", "report-dir"):
        copy.add_argument("--" + name, type=Path, required=True)
    check = commands.add_parser("verdict")
    for name in ("events", "context", "output"):
        check.add_argument("--" + name, type=Path, required=True)
    check.add_argument("--manifest", type=Path)
    check.add_argument("--expected-targets", type=Path)
    check.add_argument("--expected-analysis", type=Path, action="append", default=[])
    pack = commands.add_parser("bundle")
    for name in ("directory", "output"):
        pack.add_argument("--" + name, type=Path, required=True)
    for name in ("runner-exit", "capture-exit"):
        pack.add_argument("--" + name, type=int, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        result = capture(args.events, args.output, args.output_root, args.report_dir)
        return int(bool(result["errors"]))
    if args.command == "verdict":
        result = verdict(args.events, args.manifest or manifest_path(args.events), args.context,
                         args.expected_targets, tuple(args.expected_analysis))
        write_json(args.output, result)
        return number(result["native_exit"]) or int(result["status"] != "PASS")
    result = bundle(args.directory, args.runner_exit, args.capture_exit, args.output)
    return number(result["native_exit"]) if result["native_exit"] else args.runner_exit or int(result["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
