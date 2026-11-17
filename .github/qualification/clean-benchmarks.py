#!/usr/bin/env python3
"""Extract paired benchmark samples without compiler diagnostic configurations."""
import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('directory', type=Path)
parser.add_argument('architecture', choices=('amd64', 'arm64'))
args = parser.parse_args()
source = '5a397632d896158997ee7594866b33d741c56917'
base = 'bf5df9a1c9211afe4dbd7267c4423e9ae575f7fa'
directory = args.directory
assert (directory / 'source.txt').read_text().strip() == source
assert (directory / 'base.txt').read_text().strip() == base
commands = [line.split('\t') for line in (directory / 'commands.tsv').read_text().splitlines()]
expected_names = {'BenchmarkStartupEmpty-4'}
if args.architecture == 'amd64':
    expected_names |= {'BenchmarkSyscallbench/syscall.getpid-4', 'BenchmarkSyscallbench/syscall.getpidopt-4', 'BenchmarkSyscallUnderSeccomp/cacheable.false-4', 'BenchmarkSyscallUnderSeccomp/cacheable.true-4', 'BenchmarkForkUnderSeccomp/seccomp.none-4', 'BenchmarkForkUnderSeccomp/seccomp.cacheable-4', 'BenchmarkForkUnderSeccomp/seccomp.uncacheable-4'}
proof = {'source': source, 'base': base, 'architecture': args.architecture, 'config': 'x86_64' if args.architecture == 'amd64' else 'aarch64', 'arms': {}}
sample = re.compile(r'^(Benchmark\S+)\s+\d+\s+[0-9.eE+\-]+\s+ns/op(?:\s+.*)?$')
for arm, revision in [('base', base), ('candidate', source)]:
    for batch in range(1, 5):
        for kind in ['startup'] + (['syscallbench'] if args.architecture == 'amd64' else []):
            assert [revision, f'{arm}-{kind}-{batch}', '0'] in commands
    path = directory / f'{arm}-benchmarks.txt'
    raw = path.read_bytes()
    lines = raw.decode().splitlines()
    assert {line for line in lines if line.startswith('goarch: ')} == {f'goarch: {args.architecture}'}
    assert {line for line in lines if line.startswith('goos: ')} == {'goos: linux'}
    samples = [line for line in lines if sample.fullmatch(line)]
    counts = Counter(sample.fullmatch(line)[1] for line in samples)
    assert set(counts) == expected_names, (arm, counts)
    assert all(count == 24 for count in counts.values()), (arm, counts)
    # Keep only actual benchmark configuration lines, once. Exclude all compiler
    # diagnostics and make output that benchstat would treat as configurations.
    headers = []
    for prefix in ('goos: ', 'goarch: ', 'pkg: ', 'cpu: '):
        values = sorted({line for line in lines if line.startswith(prefix)})
        assert len(values) <= 1, (arm, prefix, values)
        headers.extend(values)
    clean = ('\n'.join(headers + samples) + '\n').encode()
    (directory / f'{arm}-benchmarks-clean.txt').write_bytes(clean)
    proof['arms'][arm] = {'revision': revision, 'rawSHA256': hashlib.sha256(raw).hexdigest(), 'cleanSHA256': hashlib.sha256(clean).hexdigest(), 'sampleCounts': dict(counts), 'headers': headers}
assert proof['arms']['base']['headers'] == proof['arms']['candidate']['headers']
(directory / 'reanalysis-identity.json').write_text(json.dumps(proof, indent=2) + '\n')
