"""Read Bazel's concatenated SpawnExec JSON without retaining the whole log."""

from collections.abc import Iterator
import json
import re
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


def program_producer(capture: dict, architecture: str, phase: str, owner: str,
                     mnemonic: str, *, path: str, size: int, sha256: str) -> dict:
    """Match one configured program to its successful remote action or cache hit."""
    match capture:
        case {"architecture": cpu, "phase": name, "status": "PASS", "image_actions": list(actions)} if (cpu, name) == (architecture, phase):
            pass
        case _:
            raise ValueError(f"Incomplete or mismatched {architecture} {phase} execution log")

    matches = [(action, output) for action in actions
               if action.get("targetLabel") == owner and action.get("mnemonic") == mnemonic
               for output in action.get("actualOutputs", ()) if output.get("path") == path]
    match matches:
        case [(action, {"digest": {"hash": str(output_hash), "sizeBytes": str(output_size), "hashFunctionName": "SHA-256" | "SHA256"}})]:
            if (output_hash, output_size) != (sha256, str(size)) or not re.fullmatch("[0-9a-f]{64}", sha256):
                raise ValueError(f"{owner} output {path} differs from the installed program")
        case _:
            raise ValueError(f"Expected one {owner} {mnemonic} output with a digest for {path}; found {len(matches)}")

    match action:
        case {"exitCode": 0, "status": "", "remotable": True, "runner": str(runner),
              "cacheHit": cached, "digest": {"hash": str(action_hash), "hashFunctionName": "SHA256" | "SHA-256"}, "full_record_sha256": str(record_hash)}:
            pass
        case _:
            raise ValueError(f"{owner} did not produce {path} successfully")
    match runner, cached:
        case ("remote", False) | ("remote cache hit", True):
            pass
        case _:
            raise ValueError(f"{owner} output {path} has no remote producer or remote cache hit")
    if not all(re.fullmatch("[0-9a-f]{64}", value) for value in (action_hash, record_hash)):
        raise ValueError(f"{owner} output {path} lacks a complete action or record digest")
    return {"owner": owner, "mnemonic": mnemonic, "output": {"path": path, "bytes": size, "sha256": sha256},
            "runner": runner, "cache_hit": cached, "action_sha256": action_hash, "full_record_sha256": record_hash}
