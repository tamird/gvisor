"""Require one exact N1 capability leaf; this does not compare performance."""

import math
import re


def require_fresh(actual_ms: int, invocation_start_ms: int) -> None:
    assert actual_ms >= invocation_start_ms, (actual_ms, invocation_start_ms)


def validate(text, expected):
    assert 'goos: linux' in text.splitlines() and 'goarch: amd64' in text.splitlines()
    assert re.search(r'^PASS$', text, re.M)
    assert not re.search(r'FAIL|SKIP|panic:|fatal error:', text)
    rows = []
    for line in text.splitlines():
        if not re.match(r'^Benchmark\S+\s+\d+\s', line):
            continue
        match = re.fullmatch(r'(Benchmark\S+)\s+(\d+)\s+([0-9.eE+-]+)\s+ns/op(?:\s+.*)?', line)
        assert match, line
        name, iterations, value = match.groups()
        assert name == expected and int(iterations) == 1, line
        value = float(value)
        assert math.isfinite(value) and value > 0, line
        rows.append({'name': name, 'iterations': 1, 'nsPerOp': value})
    assert len(rows) == 1, rows
    return rows[0]
