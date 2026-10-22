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
import shutil
import subprocess
import tarfile
import time
import zipfile

import qualification_report as report


def publish_lifecycle(results: Path, sources: dict[str, str], comparison: dict[str, object],
                      runner_exit: int, capture_exit: int, *, coordinator_source: str | None = None) -> dict[str, object]:
    """Authenticate each source bundle remotely and publish its compact result."""
    bundles = {}
    execution_finished = []
    status = comparison["status"]
    try:
        for side, source in sources.items():
            directory = results / side
            directory.mkdir(exist_ok=True)
            archive = results / (side + ".tar.gz")
            report.bundle(directory, runner_exit, capture_exit, archive)
            identity = report.file_record(archive)
            try:
                receipt = report.receipt(archive, identity["sha256"], source)
                execution_finished.extend(row["execution_finished_at"] for row in receipt["invocations"]
                                          if row["execution_finished_at"] is not None)
            except (OSError, ValueError) as error:
                receipt = {"status": "INCOMPLETE", "error": str(error)}
            path = results / (side + "-receipt.json")
            report.write_json(path, receipt)
            bundles[side] = {"path": archive.name, "status": receipt["status"], **identity,
                             "receipt": report.file_record(path)}
            if receipt["status"] != "PASS":
                status = "FAIL"
        verified = datetime.datetime.now(datetime.timezone.utc).isoformat()
        finished = max(execution_finished, default=None)
        comparison = {**comparison, "status": status, "bundles": bundles,
                      "execution_finished_at": finished, "verified_at": verified,
                      "execution_to_verified_seconds": (
                          datetime.datetime.fromisoformat(verified) -
                          datetime.datetime.fromisoformat(finished)).total_seconds() if finished else None}
        report.write_json(results / "comparison.json", comparison)
    except (OSError, ValueError) as error:
        status = "FAIL"
        report.write_json(results / "reporting-failure.json", {"error": str(error)})
    compact = results / "compact" / "qualification"
    compact.mkdir(parents=True)
    # The separately uploaded bundles retain full invocation trees. Copy only
    # small root metadata here; the manifest binds each exact audit archive.
    for path in results.iterdir():
        if path.is_file() and path.suffix in (".json", ".bench", ".txt", ".lock"):
            shutil.copyfile(path, compact / path.name)
    manifest = {"schema_version": 1, "status": status, "source": coordinator_source or sources["preflight"],
                "bundle_sources": sources, "bundles": bundles,
                "files": {path.name: report.file_record(path) for path in sorted(compact.iterdir())},
                "verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    report.write_json(compact / "manifest.json", manifest)
    return manifest


def lifecycle_receipt(artifact: Path, digest: str, source: str) -> dict[str, object]:
    """Verify the compact result without downloading its separate audit bundles."""
    identity = report.file_record(artifact)
    if identity["sha256"] != digest.removeprefix("sha256:"):
        raise ValueError("Lifecycle artifact SHA256 differs from its provider digest")
    manifest = report.mapping(json.loads(report.bundle_members(artifact, {"manifest.json"}, first=True)["manifest.json"]))
    if manifest.get("schema_version") != 1 or manifest.get("source") != source:
        raise ValueError("Lifecycle manifest schema or source differs")
    status = manifest.get("status")
    if status not in ("PASS", "FAIL"):
        raise ValueError("Unsupported lifecycle result status")
    files = {name: report.mapping(value) for name, value in report.mapping(manifest["files"]).items()}
    members = report.bundle_members(artifact, set(files), verify=files, first=True)
    bundles = report.mapping(manifest["bundles"])
    comparison = report.mapping(json.loads(members.get("comparison.json", b"{}")))
    sources = report.mapping(manifest["bundle_sources"])
    if any(re.fullmatch(r"[0-9a-f]{40}", report.string(value)) is None for value in sources.values()):
        raise ValueError("Invalid lifecycle bundle source")
    receipts = {}
    for side, raw in bundles.items():
        bundle = report.mapping(raw)
        name = side + "-receipt.json"
        if files.get(name) != bundle.get("receipt"):
            raise ValueError("Lifecycle bundle receipt is not bound by its manifest")
        receipt = report.mapping(json.loads(members[name]))
        receipts[side] = receipt
        if receipt.get("status") != bundle.get("status"):
            raise ValueError("Lifecycle receipt and bundle statuses differ")
        if receipt.get("status") == "PASS":
            retained = report.mapping(receipt["artifact"])
            if (receipt.get("sources") != [sources.get(side)] or
                    any(retained.get(key) != bundle.get(key) for key in ("sha256", "bytes"))):
                raise ValueError("Lifecycle receipt source or audit identity differs")
    if status == "PASS" and (comparison.get("status") != "PASS" or
            set(bundles) != {"before", "after", "preflight", "statistics"} or
            {side: sources.get(side) for side in ("before", "after")} != comparison.get("sources") or
            sources.get("statistics") != comparison.get("statistics_source") or
            any(row.get("status") != "PASS" for row in receipts.values())):
        raise ValueError("Passing lifecycle result has incomplete or failed receipts")
    verified = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {"schema_version": 1, "status": status, "sources": [source],
            "artifact": {"path": str(artifact), **identity}, "downloaded_bytes": identity["bytes"],
            "comparison": comparison, "bundle_receipts": receipts, "audit_bundles": bundles,
            "remote_verified_at": manifest["verified_at"], "consumer_verified_at": verified,
            "execution_to_verified_seconds": (
                datetime.datetime.fromisoformat(verified) - datetime.datetime.fromisoformat(
                    report.string(comparison["execution_finished_at"]))
            ).total_seconds() if comparison.get("execution_finished_at") else None}


def collect_actions(run: int, source: str, architecture: str, prefix: Path,
                    poll_interval: int = 10, run_attempt: int | None = None,
                    watch_seconds: int = 10000, *, lifecycle: bool = False) -> dict[str, object]:
    """Reuse gh watch/download and the canonical authenticated bundle receipt."""
    def output(suffix: str) -> Path:
        return Path(str(prefix) + suffix)

    def api(path: str) -> dict[str, object]:
        return report.mapping(json.loads(subprocess.check_output(["gh", "api", path], timeout=60)))

    workflow: dict[str, object] = {}
    bound_attempt = run_attempt
    selected_artifact: dict[str, object] | None = None
    audit_artifacts: list[dict[str, object]] = []
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
        if metadata.get("status") != "completed":
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
        if metadata.get("status") != "completed":
            raise ValueError("Workflow is still live; retain its handle rather than restarting")
        workflow["terminal_observed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        artifacts = api(path + "/artifacts")
        report.write_json(output("-artifacts.json"), artifacts)
        artifact_name = (f"state-lifecycle-result-attempt-{bound_attempt}" if lifecycle else
                         f"qualification-{architecture}-attempt-{bound_attempt}")
        accepted_names = [artifact_name]
        # Old workflows did not distinguish attempts. Their artifact is safe
        # only on the first attempt, before any earlier producer can exist.
        if bound_attempt == 1 and not lifecycle:
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
        if lifecycle:
            result = lifecycle_receipt(output("-actions.zip"), report.string(artifact["digest"]), source)
            result["producer_status"] = result["status"]
            audit_artifacts = [{key: item[key] for key in ("id", "name", "digest")}
                               for raw in report.sequence(artifacts["artifacts"])
                               if (item := report.mapping(raw)).get("name") ==
                               f"state-lifecycle-audit-attempt-{bound_attempt}" and item.get("expired") is False]
            result["audit_upload_status"] = "RETAINED" if (
                len(audit_artifacts) == 1 and audit_artifacts[0]["digest"]
            ) else "INCOMPLETE"
        else:
            result = report.receipt(output("-actions.zip"), report.string(artifact["digest"]),
                                    source, None, deadline=time.monotonic() + 180)
        result.setdefault("producer_status", result["status"])
        if result.get("audit_upload_status") == "INCOMPLETE":
            result["status"] = "INCOMPLETE"
        result["collection_status"] = "COMPLETE"
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, EOFError,
            zipfile.BadZipFile, tarfile.TarError) as error:
        result = {"schema_version": 1, "status": "INCOMPLETE",
                  "collection_status": "INCOMPLETE", "collection_error": str(error)}
    if workflow.get("status") == "completed":
        conclusion = workflow.get("conclusion")
        if conclusion in ("failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"):
            result["status"] = "FAIL"
        elif conclusion != "success" and result["status"] != "FAIL":
            result["status"] = "INCOMPLETE"
    elif result["status"] != "FAIL":
        result["status"] = "INCOMPLETE"
    result.update({"workflow": workflow, "submitted_source": source, "requested_run": run,
                   "requested_run_attempt": run_attempt, "bound_run_attempt": bound_attempt,
                   "actions_artifact": selected_artifact,
                   "audit_artifacts": audit_artifacts,
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
    parser.add_argument("--run-attempt", type=int,
                        help="Required workflow attempt; otherwise bind the first observed attempt")
    parser.add_argument("--lifecycle", action="store_true",
                        help="Fetch only the compact lifecycle result; retain separate audit upload metadata")
    parser.add_argument("--poll-interval", type=int, default=10,
                        help="Seconds between status checks; increase for concurrent runs to conserve API quota")
    parser.add_argument("--watch-seconds", type=int, default=10000,
                        help="Positive total watch budget in seconds, shared across reattachments")
    args = parser.parse_args()
    result = collect_actions(args.run, args.source, args.architecture, args.prefix,
                             args.poll_interval, args.run_attempt, args.watch_seconds, lifecycle=args.lifecycle)
    print(f"{result['status']}: {args.prefix}-receipt.json", flush=True)
    return int(result["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
