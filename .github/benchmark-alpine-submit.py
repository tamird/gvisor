"""Submit the one reviewed Alpine request and retain its public result fields."""

from datetime import datetime, timezone
import hashlib
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import sys
import tarfile
from tempfile import TemporaryDirectory
import uuid

sys.path.insert(0, str(Path("test/rbe").resolve()))
import qualification_report as report

SOURCE = "d6731da5ade2667a4077a0bac939afbccd92e8e4"
DIGEST = "8bb8c3c364c72e27b089ac3cc2b4dab7a8c7eb21568d1d4d659f4085a71e8e19"


def rpc(method: str, body: dict, key: str) -> dict:
    connection = HTTPSConnection("app.buildbuddy.io", timeout=60)
    try:
        connection.request("POST", "/rpc/BuildBuddyService/" + method, json.dumps(body),
                           {"Content-Type": "application/json", "x-buildbuddy-api-key": key})
        with connection.getresponse() as response:
            if response.status != 200:
                raise RuntimeError(f"BuildBuddy {method} returned HTTP {response.status}")
            payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024 or key.encode() in payload:
                raise ValueError("Unexpected BuildBuddy RPC response")
            return json.loads(payload)
    finally:
        connection.close()


def compact(bundle: Path) -> dict:
    result = {"phases": {}}
    with tarfile.open(bundle) as archive:
        for member in archive:
            name = member.name.removeprefix("./")
            if not member.isfile() or member.size > 2 * 1024 * 1024:
                continue
            if name == "benchmark-alpine.json":
                workload = json.load(archive.extractfile(member))
                result["workload_status"] = workload["status"]
                result["release"] = workload.get("release", {})
                result["benchmarks"] = workload.get("benchmarks", {})
                result["archives"] = {}
                for cpu, row in workload["archives"].items():
                    result["archives"][cpu] = {
                        "upstream_leaf": row["upstream_leaf"], "context": row["context"],
                        "archive_sha256": row["archive"]["sha256"], "archive_bytes": row["archive"]["bytes"],
                        "config_sha256": row["config_sha256"], "platform": [row["config"]["os"], row["config"]["architecture"]],
                        "layers": [{k: layer[k] for k in ("sha256", "bytes", "expected_diff_id")} for layer in row["layers"]],
                    }
            elif re.fullmatch(r"invocation-[a-z0-9-]+/verdict\.json", name):
                v = json.load(archive.extractfile(member))
                result["phases"][name.split("/")[0]] = {k: v[k] for k in
                    ("status", "native_exit", "case_counts", "execution_finished_at", "verdict_available_at") if k in v}
                # The public owner and attempt identity suffice; don't upload logs,
                # worker filesystem paths, or the service's private event data.
                result["phases"][name.split("/")[0]]["attempts"] = [
                    {"label": r["owner"]["label"], "status": r["status"], "cached_locally": r["cached_locally"]}
                    for r in v.get("executed", [])]
    return result


def main() -> int:
    raw = Path(".github/benchmark-alpine-request.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != DIGEST:
        raise ValueError("Request differs from the reviewed bytes")
    request = json.loads(raw)
    report.hosted_run_preflight(request)
    if request["repo_state"] != {"commit_sha": SOURCE, "branch": "qualification/benchmark-alpine-3.24.3"}:
        raise ValueError("Hosted source differs from the reviewed request")
    directory = Path(os.environ["RUNNER_TEMP"])
    key = os.environ.pop("BUILDBUDDY_API_KEY", "")
    if not key or any(not 33 <= ord(c) <= 126 for c in key):
        raise ValueError("A BuildBuddy credential is required")
    result = {"source": SOURCE, "request_sha256": DIGEST, "status": "INCOMPLETE"}
    try:
        existing = rpc("SearchInvocation", {"query": {
            "repo_url": request["git_repo"]["repo_url"], "commit_sha": SOURCE, "role": ["HOSTED_BAZEL"]},
            "count": 100}, key)
        seen = [entry for entry in existing.get("invocation", []) if entry.get("command") == request["name"]]
        if len(seen) > 1:
            raise ValueError("More than one matching execution; do not submit")
        if seen:
            invocation = seen[0].get("invocationId") or seen[0].get("invocation_id")
            result["operation"] = "attached"
        else:
            submitted = rpc("Run", request, key)
            invocation = submitted.get("invocationId") or submitted.get("invocation_id")
            result["operation"] = "submitted"
        if not invocation or str(uuid.UUID(invocation)) != invocation:
            raise ValueError("No canonical invocation ID; reconcile before resubmitting")
        result["invocation"] = invocation
        print(f"https://app.buildbuddy.io/invocation/{invocation}", flush=True)
        with TemporaryDirectory(dir=directory) as private, os.fdopen(os.memfd_create("alpine-collector"), "w+b") as fd:
            fd.write(key.encode())
            fd.flush()
            fd.seek(0)
            bundle = Path(private) / "qualification.tar.gz"
            receipt = report.native_receipt(invocation, Path(f"/proc/self/fd/{fd.fileno()}"), bundle,
                                            Path(private) / "receipt.json", SOURCE, None, 3300)
            for field in ("status", "native_exit", "collection_status", "producer_status",
                          "case_counts", "collection_finished_at", "consumer_verified_at", "verdict_available_at"):
                if field in receipt:
                    result[field] = receipt[field]
            if artifact := receipt.get("artifact"):
                result["artifact"] = {k: artifact[k] for k in ("sha256", "bytes") if k in artifact}
            if receipt.get("collection_error"):
                # The fixed-origin collector excludes keys and provider response
                # bodies from diagnostics; use its explanatory status only.
                result["collection_error"] = receipt["collection_error"]
            if receipt.get("collection_status") == "COMPLETE" and bundle.is_file():
                result.update(compact(bundle))
        if result["status"] == "PASS" and result.get("workload_status") != "PASS":
            raise ValueError("Passing receipt lacks the named Alpine workload result")
    except Exception:
        if result["status"] == "PASS":
            result["status"] = "INCOMPLETE"
        raise
    finally:
        result["recorded_at"] = datetime.now(timezone.utc).isoformat()
        data = (json.dumps(result, indent=2) + "\n").encode()
        if key.encode() in data or b"x-buildbuddy-api-key" in data.lower():
            raise ValueError("Unexpected credential in the compact result")
        (directory / "benchmark-alpine-outcome.json").write_bytes(data)
    return int(result["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
