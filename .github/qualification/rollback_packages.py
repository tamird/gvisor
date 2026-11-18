"""Acquire exact original Docker packages before any daemon/package mutation."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Protocol, TypedDict


class Package(TypedDict):
    Package: str
    Version: str
    Architecture: str
    Filename: str
    Size: str
    SHA256: str


class Command(Protocol):
    def __call__(self, *args: str, timeout: int = 30) -> str: ...


def acquire_originals(command: Command, directory: Path, versions: dict[str, str], rows: list[Package]) -> dict[str, str]:
    assert set(versions) == {'docker-ce', 'docker-ce-cli'}, 'Both installed package versions are required'
    selected: list[Package] = []
    # Validate every requested version against the frozen official metadata
    # before acquiring either package; no latest-version substitution.
    for name, version in versions.items():
        matches = [row for row in rows if row['Package'] == name and row['Version'] == version and row['Architecture'] == 'amd64']
        assert len(matches) == 1, f'No unique official rollback package for {name}={version}'
        row = matches[0]
        filename = row['Filename']
        assert filename.startswith('dists/noble/pool/stable/amd64/') and '..' not in Path(filename).parts, filename
        assert Path(filename).name.startswith(name + '_') and filename.endswith('_amd64.deb'), filename
        assert re.fullmatch(r'[0-9]+', row['Size']) and int(row['Size']) > 0, 'Invalid package size'
        assert re.fullmatch(r'[a-f0-9]{64}', row['SHA256']), 'Invalid package digest'
        selected.append(row)
    acquired: dict[str, str] = {}
    for row in selected:
        path = directory / Path(row['Filename']).name
        command('curl', '--proto', '=https', '-fsSL', '--max-time', '120',
                'https://download.docker.com/linux/ubuntu/' + row['Filename'], '-o', str(path), timeout=125)
        assert path.is_file() and not path.is_symlink(), 'Rollback package is not a regular file'
        assert path.stat().st_size == int(row['Size']), f'Wrong rollback package size: {path.name}'
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == row['SHA256'], f'Wrong rollback package digest: {path.name}'
        actual = command('dpkg-deb', '--show', '--showformat=${Package}\t${Version}\t${Architecture}', str(path)).strip()
        expected = '\t'.join((row['Package'], row['Version'], row['Architecture']))
        assert actual == expected, f'Wrong rollback package fields: {actual!r}, expected {expected!r}'
        acquired[str(path)] = digest
    assert set(directory.iterdir()) == {Path(path) for path in acquired}, 'Unexpected original-package files'
    return acquired
