"""Require the exact ordinary-path capability leaf and real positive timing."""
import json
import math
import re
import sys
from pathlib import Path

log, output = map(Path, sys.argv[1:3])
suite = sys.argv[3]
expected = {'startup': 'BenchmarkStartupEmpty-4',
            'ruby': 'BenchmarkRubyNoOpTest/page_cache.dirty/filesystem.bindfs-4'}[suite]
text = log.read_text()
assert 'goos: linux' in text.splitlines() and 'goarch: amd64' in text.splitlines()
assert re.search(r'^PASS$', text, re.M)
assert not re.search(r'FAIL|SKIP|panic:|fatal error:', text)
pattern = re.compile(r'^(Benchmark\S+)\s+(\d+)\s+([0-9.eE+-]+)\s+ns/op(?:\s+.*)?$')
rows = []
for line in text.splitlines():
    if not re.match(r'^Benchmark\S+\s+\d+\s', line):
        continue
    match = pattern.fullmatch(line)
    assert match, line
    name, iterations, value = match.groups()
    assert name == expected and int(iterations) == 1, line
    assert math.isfinite(float(value)) and float(value) > 0, line
    rows.append({'name': name, 'iterations': 1, 'nsPerOp': float(value)})
assert len(rows) == 1, rows
output.write_text(json.dumps({'scope': 'single-observation capability/cost only', 'rows': rows}, indent=2) + '\n')
