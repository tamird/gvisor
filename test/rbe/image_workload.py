"""Compare recorder gzip levels on one authenticated, redacted action prefix."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
import urllib.request
import zlib

from image_execution import compress_execution_log, execution_records
import qualification_report as report

CONFIG = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    previous = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = str(private) + (os.pathsep + previous if previous is not None else "")
    try:
        result = invoke("gzip-recording", [
            "run", "--lockfile_mode=error", "--noannounce_rc", "@rules_python//python/bin:python", "--",
            str(Path(__file__).resolve()), str(private), str(evidence),
        ], "command", targets=[])
    finally:
        if previous is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous
    if result["status"] != "PASS":
        raise RuntimeError("Paired gzip recording experiment failed")


def experiment(private: Path, evidence: Path) -> None:
    total_started = time.monotonic()
    credential = os.environ["BUILDBUDDY_API_KEY"].encode()
    if not credential:
        raise RuntimeError("Missing existing runner credential")
    supplier = CONFIG["supplier"]
    result = {"status": "INCOMPLETE", "supplier": supplier, "scope": "Reporting-only paired compression; no source qualification or compiler replay",
              "python": sys.version, "zlib": zlib.ZLIB_RUNTIME_VERSION, "machine": platform.machine(), "trials": []}
    archive, prefix = private / "supplier.json.gz", private / "prefix.json"
    try:
        started = time.monotonic()
        request = urllib.request.Request("https://app.buildbuddy.io/api/v1/GetFile",
                                         data=json.dumps({"uri": supplier["uri"]}).encode(),
                                         headers={"Content-Type": "application/json", "x-buildbuddy-api-key": credential.decode()})
        digest = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                output.write(chunk)
                if size > supplier["bytes"]:
                    raise ValueError("Supplier exceeds authenticated size")
        if (size, digest.hexdigest()) != (supplier["bytes"], supplier["sha256"]):
            raise ValueError("Supplier gzip identity mismatch")
        result["download_and_authenticate_seconds"] = time.monotonic() - started
        started = time.monotonic()
        prefix_size = 0
        with gzip.open(archive, "rt", encoding="utf-8", newline="") as stream, prefix.open("wb") as output:
            for text, action in execution_records(stream):
                raw = text.encode()
                values = re.findall(rb"x-buildbuddy-api-key=([^\s'\"]*)", raw, re.IGNORECASE)
                if credential in raw or any(value != b"[REDACTED]" for value in values):
                    raise ValueError("Supplier prefix contains credential material")
                prefix_size += len(raw)
                if prefix_size > CONFIG["prefix_max_bytes"]:
                    raise ValueError("Complete-action prefix exceeds its bound")
                output.write(raw)
                if action is not None and prefix_size >= CONFIG["prefix_min_bytes"]:
                    break
        if prefix_size < CONFIG["prefix_min_bytes"]:
            raise ValueError("Supplier has insufficient complete-action prefix")
        result["extract_prefix_seconds"] = time.monotonic() - started
        result["prefix"] = report.file_record(prefix)
        expected = None
        for index, level in enumerate((9, 1, 1, 9)):
            compressed = private / "comparison.json.gz"
            started, cpu_started = time.monotonic(), time.process_time()
            recorded = compress_execution_log(prefix, compressed, credential, tuple(CONFIG["action_prefixes"]), report.execution_summary, level)
            elapsed, cpu = time.monotonic() - started, time.process_time() - cpu_started
            stage_seconds = recorded.pop("stage_seconds")
            if recorded["uncompressed"] != result["prefix"]:
                raise ValueError("Recorder changed the authenticated prefix bytes")
            if not recorded["image_actions"]:
                raise ValueError("Prefix omitted all selected owning actions")
            if expected is None:
                expected = recorded
                report.write_json(evidence / "selected-actions.json", expected)
            if recorded != expected:
                raise ValueError("Compression changed action selection, full-input identities or uncompressed bytes")
            started = time.monotonic()
            compressed_identity = report.file_record(compressed)
            hash_seconds = time.monotonic() - started
            started = time.monotonic()
            digest, decoded_size, count, selected = hashlib.sha256(), 0, 0, []
            with gzip.open(compressed, "rt", encoding="utf-8", newline="") as stream:
                for text, action in execution_records(stream):
                    raw = text.encode()
                    digest.update(raw)
                    decoded_size += len(raw)
                    if action is not None:
                        count += 1
                        if action["targetLabel"].startswith(tuple(CONFIG["action_prefixes"])):
                            selected.append(report.execution_summary(action))
            if ({"bytes": decoded_size, "sha256": digest.hexdigest()} != expected["uncompressed"]
                    or count != sum(expected["runner_counts"].values()) or selected != expected["image_actions"]):
                raise ValueError("Decoded gzip differs from exact selected full-record identities")
            result["trials"].append({"index": index, "level": level, "recording_seconds": elapsed, "recording_cpu_seconds": cpu,
                                      "stage_seconds": stage_seconds, "compressed": compressed_identity,
                                      "compressed_hash_seconds": hash_seconds, "verify_seconds": time.monotonic() - started,
                                      "records": count, "selected_actions": len(selected)})
            compressed.unlink()
            report.write_json(evidence / "gzip-experiment.json", result)
        result["status"] = "PASS"
    finally:
        result["total_seconds"] = time.monotonic() - total_started
        report.write_json(evidence / "gzip-experiment.json", result)


if __name__ == "__main__":
    experiment(Path(sys.argv[1]), Path(sys.argv[2]))
