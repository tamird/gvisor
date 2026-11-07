"""Inventory both native variants of the tests the ARM64 presubmit executed."""

import collections
import json
import os
import pathlib
import subprocess

BASE = '35aa6228ed'
CANDIDATE = 'b8b073e3ae'
FILES = ['tools/bazeldefs/go.bzl', 'tools/bazeldefs/cc.bzl']
WORK = pathlib.Path(os.environ['RUNNER_TEMP'], 'mixed-owner-inventory')
OWNERS = pathlib.Path('.github/qualification/mixed-native-owners.txt')


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def inventory(source, owners):
    git('restore', '--source=' + source, '--worktree', '--', *FILES)
    for path in FILES:
        assert git('hash-object', path) == git('rev-parse', source + ':' + path)
    query = WORK / 'owners.query'
    query.write_text('siblings(set(\n' + '\n'.join(owners) + '\n))\n')
    result = subprocess.run(
        ['bazel', 'query', '--keep_going', '--output=label_kind',
         '--noshow_progress', '--query_file=' + str(query)],
        capture_output=True, text=True, timeout=900,
    )
    kinds = {}
    for line in result.stdout.splitlines():
        kind, _, label = line.rpartition(' ')
        if label.startswith('//'):
            kinds[label] = kind
    errors = [line for line in result.stderr.splitlines() if
              line.startswith('ERROR:') or 'previously declared' in line]
    rows = {owner: {arch: kinds.get(owner + '_' + arch)
                    for arch in ('amd64', 'arm64')} for owner in owners}
    return {
        'exit': result.returncode,
        'source': git('rev-parse', source),
        'original_kinds': dict(collections.Counter(
            kinds.get(owner, 'absent') for owner in owners)),
        'original_missing': [owner for owner in owners if owner not in kinds],
        'pairs': sum(all(variants.values()) for variants in rows.values()),
        'variant_kinds': dict(collections.Counter(
            kind for variants in rows.values() for kind in variants.values() if kind)),
        'variants': rows,
        'errors': errors[:30],
    }


def main():
    owners = OWNERS.read_text().splitlines()
    assert len(set(owners)) == len(owners) == 363
    WORK.mkdir()
    result = {'original_run': 42310008455, 'original_artifact': 13550377161,
              'owners': len(owners), 'tests_executed_here': 0}
    for name, source in [('base', BASE), ('candidate', CANDIDATE)]:
        result[name] = inventory(source, owners)
        (WORK / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
        print(name, result[name]['exit'], result[name]['pairs'],
              result[name]['original_kinds'], result[name]['errors'][:3], flush=True)
    base = result['base']
    assert base['exit'] == 0 and not base['original_missing'] and base['pairs'] >= 2
    candidate = result['candidate']
    if candidate['exit']:
        assert any('conflict' in error or 'already exists' in error or
                   'previously declared' in error for error in candidate['errors']), candidate['errors']
    else:
        assert not candidate['original_missing'] and candidate['pairs'] > base['pairs']


if __name__ == '__main__':
    main()
