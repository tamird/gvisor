# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Keep Actions credentials outside Bazel options and published artifacts."""

import argparse
import codecs
import gzip
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import BinaryIO
import zlib


HELPER = """#!/bin/sh
[ "$#" -eq 1 ] && [ "$1" = get ] || exit 2
# Bazel includes helper stderr in errors. Never print file contents on failure.
exec cat -- "$0.json" 2>/dev/null
"""


def prepare(directory: Path) -> None:
    """Create a private file-backed implementation of Bazel's helper protocol."""
    if not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
        raise ValueError("Credential directory must have mode 0700")
    key = os.environ["BUILDBUDDY_API_KEY"]
    if not key:
        raise ValueError("Missing BuildBuddy credential")
    payload = json.dumps({"headers": {"x-buildbuddy-api-key": [key]}})
    descriptor = os.open(directory / "helper.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(payload)
    descriptor = os.open(directory / "helper", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(HELPER)


def scan(stream: BinaryIO, secrets: set[bytes], *, text: bool = False,
         copy: BinaryIO | None = None) -> bytes:
    """Check literal and JSON-escaped credentials across read boundaries."""
    overlap = max(map(len, secrets)) - 1
    previous = b""
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")() if text else None
    while chunk := stream.read(1024 * 1024):
        content = previous + chunk
        if any(secret in content for secret in secrets):
            raise ValueError("Credential detected; artifact upload withheld")
        if decoder is not None:
            decoder.decode(chunk)
            if b"\x00" in chunk:
                raise ValueError("Unsupported binary evidence")
        if copy is not None:
            copy.write(chunk)
        digest.update(chunk)
        previous = content[-overlap:] if overlap else b""
    if decoder is not None:
        decoder.decode(b"", final=True)
    return digest.digest()


def scan_member(name: str, source: BinaryIO, secrets: set[bytes]) -> bytes:
    path = Path(name)
    native = len(path.parts) == 2 and path.parts[0].startswith("invocation-")
    if not native or path.name not in ("profile.json.gz", "execution.binpb"):
        if name.endswith((".zip", ".gz", ".tar", ".xz", ".bz2", ".zst", ".tgz")):
            raise ValueError("Unsupported nested archive")
        return scan(source, secrets, text=True)

    # Check the compressed bytes (including headers) and the decoded payload.
    # The subprocess reads the archived log, not a possibly different loose
    # copy left in the coordinator's workspace.
    # zstd inherits the descriptor. Buffered reads could position its file
    # descriptor beyond the frame even after Python seeks back to its start.
    with tempfile.TemporaryFile(buffering=0) as compressed:
        digest = scan(source, secrets, copy=compressed)
        compressed.seek(0)
        if path.name == "profile.json.gz":
            with gzip.GzipFile(fileobj=compressed) as profile:
                scan(profile, secrets, text=True)
        else:
            if compressed.read(4) != b"\x28\xb5\x2f\xfd":
                raise ValueError("Expected a Zstandard compact execution log")
            compressed.seek(0)
            with subprocess.Popen([os.environ.get("QUALIFICATION_ZSTD", "zstd"), "-qdc"],
                                  stdin=compressed, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL) as process:
                assert process.stdout is not None
                with process.stdout as messages:
                    scan(messages, secrets)
                if process.wait() != 0:
                    raise ValueError("Invalid compact execution log")
        return digest


def verify(directory: Path, bundle: Path, verdict: Path) -> None:
    """Verify the exact archive and separate verdict passed to upload-artifact."""
    if any(path.is_symlink() or not path.is_file() or path.resolve().is_relative_to(directory.resolve())
           for path in (bundle, verdict)):
        raise ValueError("Missing, private, or linked upload")
    credentials = json.loads((directory / "helper.json").read_text())
    values = [value for entries in credentials["headers"].values() for value in entries]
    if not values or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("Invalid credential inventory")
    secrets = {value.encode() for value in values}
    secrets.update(json.dumps(value)[1:-1].encode() for value in values)
    with verdict.open("rb") as stream:
        expected_verdict = scan(stream, secrets, text=True)
    # gzip headers may contain uncompressed filenames and comments.
    with bundle.open("rb") as stream:
        scan(stream, secrets)
    # Include tar headers and padding as well as the members tarfile exposes.
    with gzip.open(bundle, "rb") as stream:
        scan(stream, secrets)
    archived_verdict = None
    seen: set[str] = set()
    with tarfile.open(bundle, "r:gz") as archive:
        for member in archive:
            if not member.isreg() or member.name in seen:
                raise ValueError("Invalid archive member")
            seen.add(member.name)
            with archive.extractfile(member) as source:
                digest = scan_member(member.name, source, secrets)
            if member.name == "verdict.json":
                archived_verdict = digest
        # The tar writer pads the end with zeroes. Read to the gzip trailer, so
        # a partial write or a second payload cannot pass on a valid prefix.
        archive.fileobj.seek(archive.offset)
        trailing_bytes = 0
        while padding := archive.fileobj.read(1024 * 1024):
            if padding.strip(b"\x00"):
                raise ValueError("Data after final archive member")
            trailing_bytes += len(padding)
        if trailing_bytes < 2 * tarfile.BLOCKSIZE:
            raise ValueError("Missing tar end markers")
    if archived_verdict != expected_verdict:
        raise ValueError("Missing or different archived verdict")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "verify"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("bundle", type=Path, nargs="?")
    parser.add_argument("verdict", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        if args.operation == "prepare":
            prepare(args.directory)
        else:
            if args.bundle is None or args.verdict is None:
                raise ValueError("Missing artifact archive or verdict")
            verify(args.directory, args.bundle, args.verdict)
    except (EOFError, KeyError, OSError, TypeError, ValueError, tarfile.TarError, zlib.error):
        # Do not include exception text: parser or OS failures can include input.
        print("Credential preparation or artifact verification failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
