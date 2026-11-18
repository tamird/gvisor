#!/usr/bin/python3
"""Preserve canonical reports by relocating only the generated directory argument."""

import json
import os
from pathlib import Path
import re
import sys


def relocated_arguments(arguments, output, enabled):
    """Return an exact argv copy with only the canonical report path relocated."""
    positions = [index for index, value in enumerate(arguments) if value == '-coverage-report']
    assert len(positions) <= 1
    assert not any(value.startswith(('-coverage-report=', '--coverage-report')) for value in arguments)
    updated = list(arguments)
    original = None
    if positions:
        assert enabled
        index = positions[0] + 1
        assert index < len(arguments)
        original = arguments[index]
        path = Path(original)
        assert path.is_absolute()
        relative = path.resolve().relative_to(output.resolve())
        assert len(relative.parts) == 3 and relative.parts[0] == 'main.test'
        assert re.fullmatch(r'runsc[0-9]+', relative.parts[1]) and relative.parts[2] == 'coverage'
        updated[index] = str(output / 'coverage') + '/'
    return updated, original


def main():
    work = Path(os.environ['REPORT_CONSUME_WORK'])
    output = Path(os.environ['REPORT_CONSUME_OUTPUT'])
    runtime = Path(os.environ['REPORT_CONSUME_REAL_RUNTIME'])
    audit = Path(os.environ['REPORT_CONSUME_AUDIT'])
    enabled = os.environ['REPORT_CONSUME_REPORT'] == 'true'
    assert os.environ['REPORT_CONSUME_REPORT'] in ('true', 'false')
    assert work.is_absolute() and output.is_absolute() and runtime == work / 'active/runsc'
    assert re.fullmatch(r'(?:base|candidate)-(?:report-off|report-on|kcov-consume)-output', output.name)
    assert output.is_dir() and not output.is_symlink()
    assert audit.parent == output.parent and audit.name == output.name.removesuffix('-output') + '-runtime-argv.jsonl'
    arguments = sys.argv[1:]
    updated, original = relocated_arguments(arguments, output, enabled)
    if original is not None:
        destination = output / 'coverage'
        destination.mkdir(mode=0o755, exist_ok=True)
        assert destination.is_dir() and not destination.is_symlink()
    record = {'realRuntime': str(runtime), 'sidecarDirectory': os.environ['GVISOR_SIDECAR_BINARIES_DIR'], 'originalArgs': arguments, 'finalArgs': updated, 'reportPathRewritten': original is not None}
    descriptor = os.open(audit, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(json.dumps(record) + '\n')
    os.execv(runtime, [str(runtime), *updated])


if __name__ == '__main__':
    main()
