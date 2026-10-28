"""Bootstrap the pinned reporter with private, authenticated Bazel diagnostics."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import TypedDict


DIAGNOSTIC_LIMIT = 8 * 1024 * 1024


class DiagnosticRecord(TypedDict):
    sha256: str
    bytes: int
    raw_bytes: int
    retained_raw_bytes: int
    omitted_bytes: int
    retained_ranges: list[list[int]]
    truncated: bool


def diagnostic_excerpt(raw: Path, credential: bytes, limit: int = DIAGNOSTIC_LIMIT) -> tuple[bytes, DiagnosticRecord]:
    """Retain complete head/tail lines without publishing clipped credentials.

    Full phase logs belong to the qualification bundle. These are bounded
    bootstrap diagnostics; offsets and omission counts refer to raw bytes,
    while the digest identifies only the published, redacted excerpt.
    """
    if not credential or re.search(rb"[\s'\"]", credential):
        raise ValueError("Expected a nonempty single-token hosted-runner credential")
    if limit < 2:
        raise ValueError("Diagnostic limit must allow both head and tail")
    with raw.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(0)
        if size <= limit:
            data = stream.read(size)
            ranges = [[0, size]] if size else []
            retained = size
        else:
            head = stream.read(limit // 2)
            head = head[:head.rfind(b"\n") + 1]
            tail_start = size - (limit - limit // 2)
            stream.seek(tail_start - 1)
            previous = stream.read(1)
            tail = stream.read(size - tail_start)
            if previous != b"\n":
                # The first fragment may contain only a suffix of a secret.
                dropped = tail.find(b"\n") + 1 if b"\n" in tail else len(tail)
                tail_start += dropped
                tail = tail[dropped:]
            ranges = ([[0, len(head)]] if head else []) + ([[tail_start, size]] if tail else [])
            retained = len(head) + len(tail)
            marker = f"\n[bootstrap diagnostics: {size - retained} raw bytes omitted]\n".encode()
            data = head + marker + tail
    data = data.replace(credential, b"[REDACTED]")
    values = re.findall(rb"x-buildbuddy-api-key=([^\s'\"]*)", data, re.IGNORECASE)
    if credential in data or any(value != b"[REDACTED]" for value in values):
        raise ValueError("Unredacted credential marker in bootstrap diagnostics")
    return data, {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "raw_bytes": size,
        "retained_raw_bytes": retained,
        "omitted_bytes": size - retained,
        "retained_ranges": ranges,
        "truncated": size > limit,
    }


def main() -> int:
    credential = os.environ["BUILDBUDDY_API_KEY"].encode()
    if not credential or re.search(rb"[\s'\"]", credential):
        raise RuntimeError("Expected a nonempty single-token hosted-runner credential")
    source = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])["source"]
    if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != source:
        raise RuntimeError("Unexpected bootstrap source")
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"]):
        raise RuntimeError("Tracked bootstrap source is dirty")
    deadline = time.monotonic() + 2100
    os.environ["APPLICABILITY_WORK_DEADLINE"] = str(deadline)
    artifacts = Path(os.environ["BUILDBUDDY_ARTIFACTS_DIRECTORY"])
    artifacts.mkdir(parents=True, exist_ok=True)
    status = 1
    with tempfile.TemporaryDirectory(prefix="syscall-applicability-auth-") as temporary:
        private = Path(temporary)
        auth_rc = private / "credentials.bazelrc"
        with auth_rc.open("x") as output:
            os.chmod(auth_rc, 0o600)
            for option in ("remote_header", "bes_header"):
                output.write(f"common --{option}=x-buildbuddy-api-key={credential.decode()}\n")
            for option in ['--remote_executor=grpcs://remote.buildbuddy.io', '--remote_cache=grpcs://remote.buildbuddy.io', '--bes_backend=grpcs://remote.buildbuddy.io', '--bes_results_url=https://app.buildbuddy.io/invocation/', '--remote_timeout=10m', '--spawn_strategy=remote', '--noremote_local_fallback', '--jobs=400', '--remote_cache_compression', '--host_platform=@io_bazel_rules_go//go/toolchain:linux_amd64_cgo', '--remote_default_exec_properties=OSFamily=linux', '--remote_default_exec_properties=Arch=amd64', '--remote_default_exec_properties=EstimatedMemory=2GB', '--remote_default_exec_properties=workload-isolation-type=firecracker', '--remote_default_exec_properties=container-image=docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a', '--strategy=TestRunner=local', '--local_test_jobs=1', '--noremote_upload_local_results']:
                output.write("build " + option + "\n")
        os.environ["APPLICABILITY_AUTH_RC"] = str(auth_rc)
        os.environ["QUALIFICATION_NATIVE_PATH"] = os.environ["PATH"]
        os.environ["QUALIFICATION_NATIVE_BAZEL"] = subprocess.check_output(["sh", "-c", "command -v bazel"], text=True).strip()
        launch_script = private / "pinned-python.sh"
        command = ["bazel", "--bazelrc=" + str(auth_rc), "run", "--lockfile_mode=update", "--noannounce_rc", "--script_path=" + str(launch_script),
                   "@rules_python//python/bin:python", "--",
                   str(Path.cwd() / "test/rbe/image_qualification.py")]
        try:
            with (private / "stdout.log").open("wb") as stdout, (private / "stderr.log").open("wb") as stderr:
                print("Preparing the pinned Python interpreter", flush=True)
                status = subprocess.run(["timeout", "--signal=TERM", "--kill-after=5s", str(max(0.001, deadline - time.monotonic())), *command], stdout=stdout, stderr=stderr).returncode
                if status == 0:
                    if not launch_script.is_file():
                        raise RuntimeError("Bazel did not emit the pinned interpreter launch script")
                    # The pinned body enforces the same work deadline and owns the existing
                    # reporting reserve. The startup script supplies Bazel's workspace env.
                    print("Running image qualification", flush=True)
                    status = subprocess.run(["timeout", "--signal=TERM", "--kill-after=5s", str(max(0.001, deadline - time.monotonic()) + 600), str(launch_script)], stdout=stdout, stderr=stderr).returncode
        except (OSError, RuntimeError, subprocess.SubprocessError):
            status = status or 1
            raise
        finally:
            # Never replay or publish a raw rc announcement, including bootstrap failures.
            try:
                logs = {}
                for name, destination in (("stdout", sys.stdout.buffer), ("stderr", sys.stderr.buffer)):
                    data, record = diagnostic_excerpt(private / (name + ".log"), credential)
                    path = artifacts / ("bootstrap-" + name + ".log")
                    path.write_bytes(data)
                    logs[name] = record
                    destination.write(data)
                    destination.flush()
                (artifacts / "bootstrap.json").write_text(json.dumps({"source": source, "native_exit": status,
                    "headers": ["remote_header", "bes_header"], "initial_lock_mode": "update", "outputs": logs}, indent=2) + "\n")
            except (OSError, ValueError):
                status = status or 1
                raise
    return status


if __name__ == "__main__":
    sys.exit(main())
