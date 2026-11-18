"""Require all startup and Hackbench observations before coverage benchstat."""

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
    'hackbench': {'BenchmarkHackbench/ipcMode.pipe/processMode.thread-4',
                  'BenchmarkHackbench/ipcMode.socket/processMode.process-4'},
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
          'architecture': 'arm64', 'mode': 'NORMAL', 'sameRuntimePath': True,
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
            assert int(iterations) == (1 if suite == 'startup' else 20), (prefix, iterations)
            fields = line.split()
            assert len(fields) % 2 == 0, (prefix, line)
            metrics = {unit: float(value) for value, unit in zip(fields[2::2], fields[3::2])}
            assert all(math.isfinite(value) and value > 0 for value in metrics.values()), (prefix, metrics)
            if suite == 'hackbench':
                assert {'execution_time.s', 'execution_time_per_loop.s'} <= metrics.keys(), (prefix, metrics)
            counts[name] += 1
        assert set(counts) == names and set(counts.values()) == {1}, (prefix, counts)
        current_headers = [line for line in text.splitlines() if line.startswith(('goos: ', 'goarch: ', 'pkg: ', 'cpu: '))]
        assert 'goos: linux' in current_headers and 'goarch: arm64' in current_headers, (prefix, current_headers)
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
