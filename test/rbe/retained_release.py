"""Load the source-built runtime recorded by a previous qualification."""
import hashlib
import json
import os
from pathlib import Path
import tarfile
import time
import urllib.request

import qualification_report as report


def fetch(item: dict, destination: Path) -> None:
    """Fetch a source-build output and verify its declared digest and size."""
    remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Artifact retrieval exhausted the work deadline")
    request = urllib.request.Request("https://app.buildbuddy.io/api/v1/GetFile",
        data=json.dumps({"uri": item["uri"]}).encode(),
        headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    with urllib.request.urlopen(request, timeout=min(180, remaining)) as response:
        data = response.read(item["bytes"] + 1)
    if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise ValueError(f"Digest or size mismatch for {destination.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def materialize_runtime(supplier: dict, directory: Path, evidence: Path) -> dict:
    """Verify the retained release owner before fetching its seven runtime files."""
    records = {"status": "INCOMPLETE", "supplier": supplier, "files": {}}

    try:
        archive_path = directory.parent / "runtime-supplier.tar.gz"
        if not archive_path.is_file():
            fetch(supplier["bundle"], archive_path)
        if report.file_record(archive_path) != {k: supplier["bundle"][k] for k in ("bytes", "sha256")}:
            raise ValueError("Retained qualification bundle changed")
        with tarfile.open(archive_path) as archive:
            names = [entry.name for entry in archive if entry.isfile()]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate retained runtime evidence member")
            phase = supplier["phase"] + "/"
            verdict = json.load(archive.extractfile(phase + "verdict.json"))
            if verdict["source"] != supplier["source"] or verdict["status"] != "PASS" or verdict["native_exit"] != 0:
                raise ValueError("Retained runtime build lacks exact-source native success")
            owners = [row for row in verdict["expected"] if row["label"] == "//:release"]
            if owners != [supplier["owner"]]:
                raise ValueError("Retained runtime configured release owner differs")
            events = evidence / "release-supplier-events.jsonl"
            events.write_bytes(archive.extractfile(phase + "events.jsonl").read())
            report.write_json(evidence / "release-supplier-verdict.json", verdict)
        owner = report.Owner(supplier["owner"]["label"], supplier["owner"]["configuration"])
        outputs = report.advertised_build_outputs(events, {(owner, "default")})
        by_name = {row["name"].split("/release/", 1)[1]: row for row in outputs}
        expected = {"runsc", "gvisor-bin/checkpointgofer", "gvisor-bin/runsc-metric-server",
                    "gvisor-bin/runsc-fd-parking", "gvisor-bin/gvisor-sentry-prewarmer",
                    "gvisor-bin/gvisor-proc-umounter", "gvisor-bin/gvisor_sentry"}
        if len(outputs) != 8 or set(by_name) != expected | {"containerd-shim-runsc-v1"} or set(supplier["files"]) != expected:
            raise ValueError("Retained release output set differs")
        records["release_outputs"] = outputs
        for name, item in supplier["files"].items():
            if any(by_name[name][key] != item[key] for key in ("bytes", "sha256")):
                raise ValueError("Retained runtime file differs from its default-BEP output: " + name)
            # The BEP advertises materialized files. CAS availability is checked
            # here, using their authenticated content digests, without fallback.
            artifact = {**item, "uri": f"bytestream://remote.buildbuddy.io/blobs/{item['sha256']}/{item['bytes']}"}
            path = directory / name
            fetch(artifact, path)
            path.chmod(0o755)
            records["files"][name] = {**report.file_record(path), "uri": artifact["uri"]}
        records["status"] = "PASS"
        return {name: {key: row[key] for key in ("bytes", "sha256")} for name, row in records["files"].items()}
    finally:
        report.write_json(evidence / "release-supplier.json", records)
