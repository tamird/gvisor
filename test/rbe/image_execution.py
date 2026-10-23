"""Read Bazel's concatenated SpawnExec JSON without retaining the whole log."""

from collections.abc import Iterator
import json
from typing import TextIO


def require_go_link_variant(action: dict, *, race: bool, cgo: bool, linkmode: str) -> dict:
    """Require the producing GoLink action to implement the requested mode."""
    if action.get("mnemonic") != "GoLink":
        raise ValueError("Expected a native GoLink action")
    arguments = action["commandArgs"]
    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
        raise ValueError("Expected native GoLink arguments")
    modes = [arguments[index + 1] for index, value in enumerate(arguments[:-1])
             if value == "-linkmode"]
    match modes:
        case [observed_mode] if arguments[-1:] != ["-linkmode"]:
            pass
        case _:
            raise ValueError(f"Expected one complete GoLink link mode, got {modes}")
    values = [item["value"] for item in action["environmentVariables"] if item["name"] == "CGO_ENABLED"]
    match values:
        case [value] if value in ("0", "1"):
            observed_cgo = value == "1"
        case _:
            raise ValueError(f"Expected one CGO_ENABLED value, got {values}")
    if arguments.count("-race") > 1:
        raise ValueError("Repeated GoLink race option")
    observed = {"race": "-race" in arguments, "cgo": observed_cgo, "linkmode": observed_mode}
    expected = {"race": race, "cgo": cgo, "linkmode": linkmode}
    if observed != expected:
        raise ValueError(f"Native GoLink variant is {observed}, expected {expected}")
    return observed


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
