"""Read Bazel's concatenated SpawnExec JSON without retaining the whole log."""

from collections.abc import Callable, Iterator
import gzip
import hashlib
import json
from pathlib import Path
import re
import time
from typing import TextIO


def execution_records(stream: TextIO, max_record_chars: int = 64 * 1024 * 1024) -> Iterator[tuple[str, dict | None]]:
    """Yield exact text and objects, including final whitespace for audit hashing.

    The limit applies to one record, not the complete execution log. Invalid or
    oversized records fail closed; exponential reads avoid reparsing a large
    incomplete record once per line. TextIO must preserve newlines and use UTF-8.
    """
    if max_record_chars <= 0:
        raise ValueError("Execution record limit must be positive")
    decoder = json.JSONDecoder()
    pending = ""
    eof = False
    read_size = min(65536, max_record_chars + 1)
    while True:
        offset = len(pending) - len(pending.lstrip())
        if eof and offset == len(pending):
            if pending:
                yield pending, None
            return
        try:
            value, end = decoder.raw_decode(pending, offset)
        except json.JSONDecodeError:
            if eof:
                raise ValueError("Malformed or truncated native SpawnExec JSON") from None
            if len(pending) > max_record_chars:
                raise ValueError("Native SpawnExec record exceeds its character limit") from None
            chunk = stream.read(min(read_size, max_record_chars + 1 - len(pending)))
            eof = not chunk
            pending += chunk
            read_size = min(2 * read_size, max_record_chars + 1)
            continue
        if end > max_record_chars:
            raise ValueError("Native SpawnExec record exceeds its character limit")
        if not isinstance(value, dict):
            raise ValueError("Native SpawnExec record must be an object")
        yield pending[:end], value
        pending = pending[end:]
        read_size = min(65536, max_record_chars + 1)


def compress_execution_log(source: Path, destination: Path, credential: bytes,
                           prefixes: tuple[str, ...], summarize: Callable[[dict], dict],
                           level: int = 9) -> dict:
    """Stream the recorder's redaction, selection and audit bytes at one level."""
    digest = hashlib.sha256()
    record = {"runner_counts": {}, "image_actions": []}
    stages = dict.fromkeys(("decode", "redact_hash", "compress_write", "select"), 0.0)
    size = 0
    with source.open("r", encoding="utf-8", newline="") as stream, gzip.open(destination, "wb", compresslevel=level) as output:
        records = execution_records(stream)
        while True:
            started = time.monotonic()
            try:
                text, action = next(records)
            except StopIteration:
                stages["decode"] += time.monotonic() - started
                break
            stages["decode"] += time.monotonic() - started
            started = time.monotonic()
            redacted = text.replace(credential.decode(), "[REDACTED]")
            raw = redacted.encode()
            values = re.findall(rb"x-buildbuddy-api-key=([^\s'\"]*)", raw, re.IGNORECASE)
            if credential in raw or any(value != b"[REDACTED]" for value in values):
                raise ValueError("Credential bytes or unrecognized API-key value in qualification evidence")
            digest.update(raw)
            size += len(raw)
            stages["redact_hash"] += time.monotonic() - started
            started = time.monotonic()
            output.write(raw)
            stages["compress_write"] += time.monotonic() - started
            started = time.monotonic()
            if action is not None:
                if redacted != text:
                    action = json.loads(redacted)
                runner = action["runner"]
                record["runner_counts"][runner] = record["runner_counts"].get(runner, 0) + 1
                if action["targetLabel"].startswith(prefixes):
                    record["image_actions"].append(summarize(action))
            stages["select"] += time.monotonic() - started
        started = time.monotonic()
    stages["compress_write"] += time.monotonic() - started
    return {**record, "uncompressed": {"bytes": size, "sha256": digest.hexdigest()}, "stage_seconds": stages}
