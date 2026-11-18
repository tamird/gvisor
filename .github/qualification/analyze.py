"""Validate all scheduled benchmark samples before running benchstat."""

import hashlib
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
schedule = json.loads((root / 'schedule.json').read_text())['orders']
labels = {'base', 'candidate', 'controlA', 'controlB'}
assert len(schedule) == 4
assert all(len(order) == 4 and set(order) == labels for order in schedule)
assert all({order[position] for order in schedule} == labels for position in range(4))
pattern = re.compile(r'^(?P<benchmark>Benchmark\S+)\s+(?P<iterations>\d+)\s+(?P<ns>[0-9.eE+\-]+)\s+ns/op(?:\s+.*)?$')
expected = {
    'syscallbench': {'BenchmarkSyscallbench/syscall.getpid-4', 'BenchmarkSyscallbench/syscall.getpidopt-4'},
    'startup': {'BenchmarkStartupEmpty-4'},
}
proof = {'priorAdverseRunsRetained': ['44190929074', '44196112563'],
         'note': 'A null result here does not supersede earlier adverse measurements.', 'labels': {}}
for label in sorted(labels):
    per_suite = {}
    for suite, cases in expected.items():
        samples = []
        headers = None
        hashes = {}
        for batch in range(1, 5):
            for kind, count in (('warmup', 3), ('measured', 6)):
                path = root / f'{label}-{batch}-{suite}-{kind}.log'
                raw = path.read_text()
                if '--- FAIL:' in raw or '--- SKIP:' in raw or 'FAIL\n' in raw:
                    raise ValueError(f'Failed or skipped benchmark in {path.name}')
                lines = raw.splitlines()
                actual = [line for line in lines if pattern.fullmatch(line)]
                for line in actual:
                    match = pattern.fullmatch(line)
                    iterations = int(match['iterations'])
                    duration = float(match['ns'])
                    if iterations <= 0 or not math.isfinite(duration) or duration <= 0:
                        raise ValueError(f'Invalid iterations or ns/op in {path.name}: {line}')
                counts = Counter(pattern.fullmatch(line)[1] for line in actual)
                if set(counts) != cases or set(counts.values()) != {count}:
                    raise ValueError(f'Incorrect actual sample accounting: {path.name}: {counts}')
                current_headers = [line for line in lines if line.startswith(('goos: ', 'goarch: ', 'cpu: ', 'pkg: '))]
                if 'goos: linux' not in current_headers or 'goarch: amd64' not in current_headers:
                    raise ValueError(f'Missing worker headers: {path.name}')
                if headers is None:
                    headers = current_headers
                if headers != current_headers:
                    raise ValueError(f'Worker/header identity changed: {path.name}')
                hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
                if kind == 'measured':
                    samples.extend(actual)
        counts = Counter(pattern.fullmatch(line)[1] for line in samples)
        assert set(counts.values()) == {24}, (label, suite, counts)
        output = root / f'{label}-{suite}-clean.txt'
        output.write_text('\n'.join(headers + samples) + '\n')
        per_suite[suite] = {'sampleCounts': dict(counts), 'rawSHA256': hashes,
                            'cleanSHA256': hashlib.sha256(output.read_bytes()).hexdigest(), 'headers': headers}
    proof['labels'][label] = per_suite
(root / 'sample-accounting.json').write_text(json.dumps(proof, indent=2) + '\n')
