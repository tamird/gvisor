"""Verify complete filesets and retain reproducibility failures before timing."""

import hashlib
import json
import stat
import sys
from pathlib import Path


def fingerprint(root: Path) -> dict[str, dict[str, object]]:
    records = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Unexpected symlink in concrete release: {path}')
        if path.is_file():
            records[str(path.relative_to(root))] = {
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'size': path.stat().st_size, 'mode': stat.S_IMODE(path.stat().st_mode),
            }
        elif not path.is_dir():
            raise ValueError(f'Unexpected special file: {path}')
    if not records:
        raise ValueError(f'Empty release: {root}')
    return records


def main() -> None:
    operation = sys.argv[1]
    if operation == 'snapshot':
        release, output = map(Path, sys.argv[2:])
        output.write_text(json.dumps({'actual': fingerprint(release)}, indent=2) + '\n')
    elif operation == 'freeze':
        release, expected, output = map(Path, sys.argv[2:])
        actual = fingerprint(release)
        prior = json.loads(expected.read_text())
        differences = {
            name: {'expected': prior.get(name), 'actual': actual.get(name)}
            for name in prior.keys() | actual.keys()
            if name not in prior or name not in actual or
            prior[name].get('sha256') != actual[name].get('sha256') or
            prior[name].get('size') != actual[name].get('size')
        }
        output.write_text(json.dumps({'actual': actual, 'differencesFromPriorLayoutRun': differences,
                                     'reproducible': not differences}, indent=2) + '\n')
        if differences:
            raise SystemExit('Full release does not reproduce prior ELF hashes; measurement blocked. Inspect retained complete archives and section inventories; do not substitute a new baseline silently.')
    elif operation == 'verify':
        release, frozen, output = map(Path, sys.argv[2:])
        actual = fingerprint(release)
        expected = json.loads(frozen.read_text())['actual']
        output.write_text(json.dumps({'actual': actual, 'matchesFrozenFileset': actual == expected}, indent=2) + '\n')
        if actual != expected:
            raise SystemExit('Complete active fileset differs from frozen release')
    else:
        raise ValueError(f'Unknown operation: {operation}')


if __name__ == '__main__':
    main()
