"""Account for every exact-source iperf sample before statistical analysis."""

from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

root, catalog = map(Path, sys.argv[1:])
expected = {name + '-4' for name in json.loads(catalog.read_text())}
assert len(expected) == 8, expected
schedule = json.loads((root / 'schedule.json').read_text())
planned = [{'kind': 'warmup', 'number': 0, 'arm': arm} for arm in ('base', 'candidate')]
for pair in range(1, 25):
    order = ('base', 'candidate') if pair % 2 else ('candidate', 'base')
    planned.extend({'kind': 'measured', 'number': pair, 'arm': arm} for arm in order)
assert schedule['visits'] == planned, schedule
statuses = [line.split('\t') for line in (root / 'status.tsv').read_text().splitlines()]
rows_by_arm = {'base': [], 'candidate': []}
proof = {'source': 'f9446ddf973def4ce653acbb098532c2b1eb0ef0',
         'base': '1f33fe3003455db178b97f3b43d3a012f1611994',
         'harnessSource': '1f33fe3003455db178b97f3b43d3a012f1611994',
         'plan': os.environ['PLAN'], 'architecture': 'amd64', 'sameActivePath': True, 'samples': {}}
common_headers = None
for visit in planned:
    kind, number, arm = visit['kind'], visit['number'], visit['arm']
    prefix = f'{kind}-{number}-{arm}'
    assert [prefix + '-visit', '0'] in statuses, (prefix, statuses)
    path = root / (prefix + '.log')
    raw = path.read_bytes()
    lines = raw.decode().splitlines()
    assert not any(re.search(r'(^|\s)(FAIL|SKIP)(?:\s|$)', line) for line in lines), prefix
    samples = [line for line in lines if line.startswith('Benchmark')]
    actual = Counter()
    for line in samples:
        parts = line.split()
        assert len(parts) >= 8 and len(parts) % 2 == 0, (prefix, parts)
        name, iterations = parts[:2]
        assert name in expected and int(iterations) == 1048576, (prefix, parts)
        metrics = {unit: float(value) for value, unit in zip(parts[2::2], parts[3::2])}
        assert {'ns/op', 'MB/s', 'bandwidth.bytes_per_second'} <= metrics.keys(), (prefix, metrics)
        assert all(math.isfinite(value) and value > 0 for value in metrics.values()), (prefix, metrics)
        actual[name] += 1
    assert set(actual) == expected and set(actual.values()) == {1}, (prefix, actual)
    headers = [line for line in lines if line.startswith(('goos: ', 'goarch: ', 'pkg: ', 'cpu: '))]
    assert 'goos: linux' in headers and 'goarch: amd64' in headers, (prefix, headers)
    if common_headers is None:
        common_headers = headers
    assert headers == common_headers, (prefix, headers, common_headers)
    for artifact in ('active-before', 'active-after', 'harness-before', 'harness-after'):
        assert json.loads((root / f'{prefix}-{artifact}.json').read_text())['matchesFrozenFileset'], (prefix, artifact)
    if kind == 'measured':
        rows_by_arm[arm].extend(samples)
    proof['samples'][prefix] = {'kind': kind, 'rawSHA256': hashlib.sha256(raw).hexdigest(), 'caseCounts': dict(actual)}
for arm, rows in rows_by_arm.items():
    counts = Counter(line.split()[0] for line in rows)
    assert set(counts) == expected and set(counts.values()) == {24}, (arm, counts)
    (root / f'{arm}-clean.txt').write_text('\n'.join(common_headers + rows) + '\n')
proof['harnessIdentity'] = json.loads((root / 'harness-identity.json').read_text())
proof['runtimeFilesets'] = {arm: json.loads((root / arm / 'frozen-fileset.json').read_text()) for arm in rows_by_arm}
(root / 'study-evidence.json').write_text(json.dumps(proof, indent=2) + '\n')
