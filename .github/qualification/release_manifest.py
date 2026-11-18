#!/usr/bin/env python3
"""Record and verify an opaque extracted release without archive UID assumptions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat


def snapshot(root: Path) -> dict[str, dict[str, str | int]]:
    entries: dict[str, dict[str, str | int]] = {}

    def visit(path: Path) -> None:
        metadata = path.lstat()
        name = path.relative_to(root).as_posix()
        entry: dict[str, str | int] = {"mode": stat.S_IMODE(metadata.st_mode)}
        if stat.S_ISLNK(metadata.st_mode):
            entry.update(type="symlink", target=os.readlink(path))
        elif stat.S_ISREG(metadata.st_mode):
            with path.open("rb") as stream:
                hasher = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    hasher.update(chunk)
                digest = hasher.hexdigest()
            entry.update(type="file", sha256=digest)
        elif stat.S_ISDIR(metadata.st_mode):
            entry.update(type="directory")
        else:
            raise ValueError(f"Unsupported release member type: {name}")
        entries[name] = entry
        if entry["type"] == "directory":
            with os.scandir(path) as children:
                paths = sorted(Path(child.path) for child in children)
            for child in paths:
                visit(child)

    visit(root)
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("record", "verify"))
    parser.add_argument("root", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    actual = snapshot(args.root)
    encoded = json.dumps(actual, indent=2, sort_keys=True) + "\n"
    if args.operation == "record":
        with args.manifest.open("x") as stream:
            stream.write(encoded)
    else:
        # Compare canonical encodings; no unchecked manifest schema decoding.
        expected = args.manifest.read_text()
        if encoded != expected:
            raise SystemExit("Extracted release differs from its frozen manifest")


if __name__ == "__main__":
    main()
