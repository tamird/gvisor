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

"""Collect one existing Actions qualification without losing workflow failures."""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import re
import subprocess
import tarfile
import time
import zipfile

import qualification_report as report


def collect_actions(run: int, source: str, architecture: str, prefix: Path,
                    poll_interval: int = 10, run_attempt: int | None = None,
                    watch_seconds: int = 10000, row_id: str | None = None) -> dict[str, object]:
    """Reuse gh watch/download and the canonical authenticated bundle receipt."""
    def output(suffix: str) -> Path:
        return Path(str(prefix) + suffix)

    def api(path: str) -> dict[str, object]:
        return report.mapping(json.loads(subprocess.check_output(["gh", "api", path], timeout=60)))

    workflow: dict[str, object] = {}
    bound_attempt = run_attempt
    selected_artifact: dict[str, object] | None = None
    watch_exit = None
    watch_exits: list[int] = []
    attached_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report.write_json(output("-collector.json"), {
        "run": run, "source": source, "collector_attached_at": attached_at,
        "poll_interval_seconds": poll_interval, "requested_run_attempt": run_attempt,
        "watch_seconds": watch_seconds,
    })

    def observe(metadata: dict[str, object]) -> None:
        nonlocal bound_attempt
        workflow.clear()
        workflow.update({key: metadata[key] for key in (
            "id", "head_sha", "status", "conclusion", "html_url", "run_attempt",
            "created_at", "updated_at",
        ) if key in metadata})
        report.write_json(output("-actions.json"), metadata)
        if type(metadata.get("id")) is not int or metadata["id"] != run:
            raise ValueError("Actions returned a different or malformed workflow run")
        if metadata.get("head_sha") != source:
            raise ValueError("Workflow source differs from the requested source")
        attempt = metadata.get("run_attempt")
        if type(attempt) is not int or attempt <= 0:
            raise ValueError("Workflow run_attempt must be a positive integer")
        if bound_attempt is None:
            bound_attempt = attempt
        elif attempt != bound_attempt:
            raise ValueError(f"Workflow attempt changed or differs from the request: expected {bound_attempt}, got {attempt}")

    try:
        if type(run) is not int or run <= 0 or re.fullmatch(r"[0-9a-f]{40}", source) is None:
            raise ValueError("Expected a positive workflow run ID and full source SHA")
        if architecture not in ("amd64", "arm64", "all"):
            raise ValueError("Unknown qualification architecture")
        if type(poll_interval) is not int or poll_interval <= 0:
            raise ValueError("Expected a positive polling interval")
        if type(watch_seconds) is not int or watch_seconds <= 0:
            raise ValueError("Expected a positive watch budget in seconds")
        if run_attempt is not None and (type(run_attempt) is not int or run_attempt <= 0):
            raise ValueError("Expected a positive workflow run attempt")
        path = f"repos/tamird/gvisor/actions/runs/{run}"
        metadata = api(path)
        observe(metadata)
        if row_id is None and metadata.get("status") != "completed":
            deadline = time.monotonic() + watch_seconds
            with output("-watch.log").open("w") as log:
                # A disconnected watch does not stop the workflow. Reattach
                # within the original deadline, preserving all watch output.
                for attempt in range(3):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    log.write(f"Watch attempt {attempt + 1}\n")
                    log.flush()
                    watch = subprocess.run(
                        ["gh", "run", "watch", str(run), "--repo", "tamird/gvisor",
                         "--interval", str(poll_interval), "--exit-status"],
                        stdout=log, stderr=subprocess.STDOUT, timeout=remaining,
                    )
                    watch_exit = watch.returncode
                    watch_exits.append(watch_exit)
                    metadata = api(path)
                    observe(metadata)
                    if metadata.get("status") == "completed":
                        break
        if row_id is None and metadata.get("status") != "completed":
            raise ValueError("Workflow is still live; retain its handle rather than restarting")
        if metadata.get("status") == "completed":
            workflow["terminal_observed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        artifacts = api(path + "/artifacts")
        report.write_json(output("-artifacts.json"), artifacts)
        label = architecture if row_id is None else "row-" + row_id
        artifact_name = f"qualification-{label}-attempt-{bound_attempt}"
        accepted_names = [artifact_name]
        # Old workflows did not distinguish attempts. Their artifact is safe
        # only on the first attempt, before any earlier producer can exist.
        if row_id is None and bound_attempt == 1:
            accepted_names.append("qualification-" + architecture)
        rows = [report.mapping(item) for item in report.sequence(artifacts["artifacts"])
                if report.mapping(item).get("name") in accepted_names
                and report.mapping(item).get("expired") is False]
        match rows:
            case [artifact]:
                pass
            case _:
                raise ValueError(f"Expected one authenticated {artifact_name} artifact, got {len(rows)}")
        if not artifact.get("digest"):
            raise ValueError("Qualification artifact is missing its SHA256 digest")
        if type(artifact.get("id")) is not int or artifact["id"] <= 0:
            raise ValueError("Qualification artifact has an invalid ID")
        selected_artifact = {key: artifact[key] for key in ("id", "name", "digest")}
        with output("-actions.zip").open("wb") as stream:
            subprocess.run(
                ["gh", "api", f"repos/tamird/gvisor/actions/artifacts/{artifact['id']}/zip"],
                stdout=stream, check=True, timeout=600,
            )
        result = report.receipt(output("-actions.zip"), report.string(artifact["digest"]),
                                source, None, deadline=time.monotonic() + 180)
        if row_id is not None:
            metadata = report.mapping(result.get("matrix_row"))
            row = report.mapping(metadata.get("row"))
            if (metadata.get("source") != source or metadata.get("run_id") != str(run)
                    or metadata.get("run_attempt") != bound_attempt
                    or row.get("id") != row_id or row.get("architecture") != architecture):
                raise ValueError("Published row does not match the requested source, run, attempt, ID and architecture")
        result["producer_status"] = result["status"]
        result["collection_status"] = "COMPLETE"
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, EOFError,
            zipfile.BadZipFile, tarfile.TarError) as error:
        result = {"schema_version": 1, "status": "INCOMPLETE",
                  "collection_status": "INCOMPLETE", "collection_error": str(error)}
    if row_id is not None:
        result["scope"] = "row"
        result["requested_row"] = row_id
    if workflow.get("status") == "completed":
        conclusion = workflow.get("conclusion")
        if conclusion in ("failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"):
            result["status"] = "FAIL"
        elif conclusion != "success" and result["status"] != "FAIL":
            result["status"] = "INCOMPLETE"
    elif result["status"] != "FAIL":
        result["status"] = "INCOMPLETE"
    if row_id is not None and result["status"] == "PASS":
        # Only the composed artifact can establish that every planned row ran.
        result["status"] = "INCOMPLETE"
    result.update({"workflow": workflow, "submitted_source": source, "requested_run": run,
                   "requested_run_attempt": run_attempt, "bound_run_attempt": bound_attempt,
                   "actions_artifact": selected_artifact,
                   "source_verified": type(workflow.get("id")) is int and workflow["id"] == run
                   and workflow.get("head_sha") == source,
                   "watch_exit": watch_exit, "watch_exits": watch_exits, "collector_attached_at": attached_at,
                   "poll_interval_seconds": poll_interval, "watch_seconds": watch_seconds,
                   "collection_finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    report.write_json(output("-receipt.json"), result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64", "all"), required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--row", help="Read this planned row once, without waiting for the workflow. "
                        "Its producer_status applies to the row only.")
    parser.add_argument("--run-attempt", type=int,
                        help="Required workflow attempt; otherwise bind the first observed attempt")
    parser.add_argument("--poll-interval", type=int, default=10,
                        help="Seconds between status checks; increase for concurrent runs to conserve API quota")
    parser.add_argument("--watch-seconds", type=int, default=10000,
                        help="Positive total watch budget in seconds, shared across reattachments")
    args = parser.parse_args()
    result = collect_actions(args.run, args.source, args.architecture, args.prefix,
                             args.poll_interval, args.run_attempt, args.watch_seconds, args.row)
    status = (result.get("producer_status", "INCOMPLETE")
              if args.row is not None else result["status"])
    print(f"{'Row ' + args.row + ': ' if args.row is not None else ''}{status}: {args.prefix}-receipt.json", flush=True)
    return int(status != "PASS" or result["status"] == "FAIL")


if __name__ == "__main__":
    raise SystemExit(main())
