"""Validate one complete existing portable or x86 coverage capability suite."""

import json
import math
from pathlib import Path
import re
import sys


path, output = map(Path, sys.argv[1:3])
suite, architecture = sys.argv[3:]
catalog = {
    'startup': {'BenchmarkStartupEmpty-4'},
    'syscall': {
        'BenchmarkSyscallbench/syscall.getpid-4', 'BenchmarkSyscallbench/syscall.getpidopt-4',
        'BenchmarkSyscallUnderSeccomp/cacheable.false-4', 'BenchmarkSyscallUnderSeccomp/cacheable.true-4',
        'BenchmarkForkUnderSeccomp/seccomp.none-4', 'BenchmarkForkUnderSeccomp/seccomp.cacheable-4',
        'BenchmarkForkUnderSeccomp/seccomp.uncacheable-4',
    },
    'hackbench': {'BenchmarkHackbench/ipcMode.pipe/processMode.thread-4',
                  'BenchmarkHackbench/ipcMode.socket/processMode.process-4'},
}
text = path.read_text()
assert 'goos: linux' in text.splitlines() and 'goarch: ' + architecture in text.splitlines(), path
assert not re.search(r'(^|\n)(FAIL|--- FAIL:|--- SKIP:)', text), path
rows = [line.split() for line in text.splitlines() if line.startswith('Benchmark')]
assert len(rows) == len(catalog[suite]) and {row[0] for row in rows} == catalog[suite], (path, rows)
records = []
for parts in rows:
    assert len(parts) >= 4 and len(parts) % 2 == 0, (path, parts)
    name, iterations = parts[:2]
    assert int(iterations) > 0, (path, parts)
    if suite != 'syscall':
        assert int(iterations) == 1, (path, parts)
    metrics = {unit: float(value) for value, unit in zip(parts[2::2], parts[3::2])}
    assert 'ns/op' in metrics and all(math.isfinite(value) and value > 0 for value in metrics.values()), (path, metrics)
    if suite == 'hackbench':
        assert {'execution_time.s', 'execution_time_per_loop.s'} <= metrics.keys(), (path, metrics)
    records.append({'benchmark': name, 'iterations': int(iterations), 'metrics': metrics})
output.write_text(json.dumps({'suite': suite, 'architecture': architecture, 'capabilityOnly': True,
                             'observations': records}, indent=2) + '\n')
