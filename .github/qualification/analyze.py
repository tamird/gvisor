"""Require all startup and syscall observations before coverage benchstat."""

from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys


root = Path(sys.argv[1])
expected = {
    'startup': {'BenchmarkStartupEmpty-4'},
    'syscall': {
        'BenchmarkSyscallbench/syscall.getpid-4',
        'BenchmarkSyscallbench/syscall.getpidopt-4',
        'BenchmarkSyscallUnderSeccomp/cacheable.false-4',
        'BenchmarkSyscallUnderSeccomp/cacheable.true-4',
        'BenchmarkForkUnderSeccomp/seccomp.none-4',
        'BenchmarkForkUnderSeccomp/seccomp.cacheable-4',
        'BenchmarkForkUnderSeccomp/seccomp.uncacheable-4',
    },
}
visits = [{'kind': 'warmup', 'pair': 0, 'side': side} for side in ('base', 'candidate')]
for pair in range(1, 25):
    order = ('base', 'candidate') if pair % 2 else ('candidate', 'base')
    visits.extend({'kind': 'measured', 'pair': pair, 'side': side} for side in order)
assert json.loads((root / 'schedule.json').read_text())['visits'] == visits
stages = [line.split('\t') for line in (root / 'stages.tsv').read_text().splitlines()]
clean = {side: {suite: [] for suite in expected} for side in ('base', 'candidate')}
headers = {}
report = {'source': '7eafb7afe2c0255c98392c2e701ec6aecad22b27',
          'base': '1f33fe3003455db178b97f3b43d3a012f1611994', 'plan': os.environ['PLAN'],
          'architecture': 'amd64', 'mode': 'NORMAL', 'sameRuntimePath': True,
          'harnessSource': '1f33fe3003455db178b97f3b43d3a012f1611994', 'visits': {}}
pattern = re.compile(r'^(Benchmark\S+)\s+(\d+)\s+([0-9.eE+-]+)\s+ns/op(?:\s+.*)?$')
for visit in visits:
    prefix = f"{visit['kind']}-{visit['pair']}-{visit['side']}"
    assert any(len(row) == 4 and row[0] == prefix + '-visit' and row[1] == 'END' and row[3] == '0' for row in stages), prefix
    report['visits'][prefix] = {}
    for suite, names in expected.items():
        path = root / f'{prefix}-{suite}.log'
        raw = path.read_bytes()
        text = raw.decode()
        assert not re.search(r'(^|\n)(FAIL|--- FAIL:|--- SKIP:)', text), prefix
        rows = [line for line in text.splitlines() if line.startswith('Benchmark')]
        counts = Counter()
        for line in rows:
            match = pattern.fullmatch(line)
            assert match is not None, (prefix, line)
            name, iterations, ns = match.groups()
            assert name in names and int(iterations) > 0 and math.isfinite(float(ns)) and float(ns) > 0, (prefix, line)
            if suite == 'startup':
                assert int(iterations) == 1, (prefix, iterations)
            counts[name] += 1
        assert set(counts) == names and set(counts.values()) == {1}, (prefix, counts)
        current_headers = [line for line in text.splitlines() if line.startswith(('goos: ', 'goarch: ', 'pkg: ', 'cpu: '))]
        assert 'goos: linux' in current_headers and 'goarch: amd64' in current_headers, (prefix, current_headers)
        headers.setdefault(suite, current_headers)
        assert headers[suite] == current_headers, (prefix, current_headers)
        if visit['kind'] == 'measured':
            clean[visit['side']][suite].extend(rows)
        report['visits'][prefix][suite] = {'cases': dict(counts), 'rawSha256': hashlib.sha256(raw).hexdigest()}
    for part in ('runtime-before', 'runtime-after', 'harness-before', 'harness-after'):
        assert json.loads((root / f'{prefix}-{part}.json').read_text())['matchesFrozenFileset'], (prefix, part)
for side, suites in clean.items():
    for suite, rows in suites.items():
        counts = Counter(pattern.fullmatch(line)[1] for line in rows)
        assert set(counts) == expected[suite] and set(counts.values()) == {24}, (side, suite, counts)
        (root / f'{side}-{suite}-clean.txt').write_text('\n'.join(headers[suite] + rows) + '\n')
(root / 'study-evidence.json').write_text(json.dumps(report, indent=2) + '\n')
