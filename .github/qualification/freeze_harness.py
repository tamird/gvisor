"""Freeze canonical benchmark executables and their complete runfiles trees."""

import hashlib
import json
import shutil
import stat
import struct
import sys
from pathlib import Path

paths, destination, report = map(Path, sys.argv[1:])
records = {}
for line in paths.read_text().splitlines():
    binary = Path(line)
    if not line.startswith('bazel-out/') or not binary.is_file():
        raise ValueError(f'Unexpected canonical cquery executable: {line}')
    data = binary.read_bytes()
    if data[:6] != b'\x7fELF\x02\x01':
        raise ValueError(f'Expected AMD64 ELF64 benchmark executable: {binary}')
    header = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
    if header[2] != 62:
        raise ValueError(f'Expected AMD64 architecture: {binary}')
    for index in range(header[10]):
        offset = header[5] + index * header[9]
        if struct.unpack_from('<I', data, offset)[0] == 3:
            raise ValueError(f'Benchmark requires a dynamic interpreter: {binary}')
    runfiles = Path(str(binary) + '.runfiles')
    if not runfiles.is_dir():
        raise ValueError(f'Canonical benchmark runfiles missing: {runfiles}')
    name = binary.name
    if name not in ('syscallbench_test', 'startup_test') or name in records:
        raise ValueError(f'Unexpected or duplicate benchmark: {name}')
    shutil.copy2(binary, destination / name)
    target_runfiles = destination / (name + '.runfiles')
    shutil.copytree(runfiles, target_runfiles, symlinks=False)
    workspace = '_main'
    if not (target_runfiles / workspace).is_dir():
        raise ValueError(f'Expected canonical _main runfiles workspace: {runfiles}')
    records[name] = {'sourcePath': line, 'sha256': hashlib.sha256(data).hexdigest(),
                     'staticallyLinked': True, 'runfiles': str(target_runfiles),
                     'testWorkspace': workspace}
if set(records) != {'syscallbench_test', 'startup_test'}:
    raise ValueError(f'Expected exactly both benchmark binaries: {records.keys()}')
report.write_text(json.dumps(records, indent=2) + '\n')
