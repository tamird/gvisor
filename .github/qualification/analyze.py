#!/usr/bin/env python3
"""Bind all controlled-replication samples to their immutable inputs."""
import argparse
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('results', type=Path)
args = parser.parse_args()
root = args.results
source = '5a397632d896158997ee7594866b33d741c56917'
base = 'bf5df9a1c9211afe4dbd7267c4423e9ae575f7fa'
commands = [line.split('\t') for line in (root / 'commands.tsv').read_text().splitlines()]
schedule = json.loads((root / 'schedule.json').read_text())
labels = {'base', 'candidate', 'controlA', 'controlB'}
assert len(schedule['orders']) == 4, schedule
assert all(set(order) == labels and len(order) == 4 for order in schedule['orders']), schedule
assert all(len({order[position] for order in schedule['orders']}) == 4 for position in range(4)), schedule
pattern = re.compile(r'^(Benchmark\S+)\s+\d+\s+[0-9.eE+\-]+\s+ns/op(?:\s+.*)?$')
expected = {'BenchmarkSyscallbench/syscall.getpid-4', 'BenchmarkSyscallbench/syscall.getpidopt-4'}
proof = {'source': source, 'base': base, 'plan': os.environ['PLAN'], 'architecture': 'amd64', 'config': 'x86_64', 'originalEvidence': 'https://github.com/tamird/gvisor/actions/runs/44190929074', 'note': 'Independent replication; original +3.22% optimized-getpid result is retained, not superseded. A/A labels share the identical frozen base runsc binary.', 'arms': {}}
common_headers = None
for label in sorted(labels):
    revision = source if label == 'candidate' else base
    samples = []
    arm_headers = None
    hashes = {}
    for batch in range(1, 5):
        assert [revision, f'{label}-warmup-{batch}', '0'] in commands, (label, batch, 'warmup')
        assert [revision, f'{label}-measured-{batch}', '0'] in commands, (label, batch, 'measured')
        raw = (root / f'{label}-measured-{batch}.log').read_bytes()
        hashes[str(batch)] = hashlib.sha256(raw).hexdigest()
        lines = raw.decode().splitlines()
        batch_samples = [line for line in lines if pattern.fullmatch(line)]
        counts = Counter(pattern.fullmatch(line)[1] for line in batch_samples)
        assert set(counts) == expected and set(counts.values()) == {6}, (label, batch, counts)
        headers = []
        for prefix in ('goos: ', 'goarch: ', 'pkg: ', 'cpu: '):
            values = sorted({line for line in lines if line.startswith(prefix)})
            assert len(values) <= 1, (label, batch, prefix, values)
            headers.extend(values)
        assert 'goos: linux' in headers and 'goarch: amd64' in headers, headers
        if arm_headers is None:
            arm_headers = headers
        assert headers == arm_headers, (label, batch, headers, arm_headers)
        samples.extend(batch_samples)
    if common_headers is None:
        common_headers = arm_headers
    assert arm_headers == common_headers, (label, arm_headers, common_headers)
    counts = Counter(pattern.fullmatch(line)[1] for line in samples)
    assert set(counts.values()) == {24}, (label, counts)
    clean = ('\n'.join(arm_headers + samples) + '\n').encode()
    (root / f'{label}-clean.txt').write_bytes(clean)
    runtime_arm = 'candidate' if label == 'candidate' else 'base'
    proof['arms'][label] = {'revision': revision, 'runtimeSHA256': (root / f'{runtime_arm}-runsc.sha256').read_text().split()[0], 'rawBatchSHA256': hashes, 'cleanSHA256': hashlib.sha256(clean).hexdigest(), 'sampleCounts': dict(counts), 'headers': arm_headers}
assert proof['arms']['controlA']['runtimeSHA256'] == proof['arms']['controlB']['runtimeSHA256'] == proof['arms']['base']['runtimeSHA256']
(root / 'replication-identity.json').write_text(json.dumps(proof, indent=2) + '\n')
