"""Freeze complete canonical syscall wrapper runfiles without executing them."""
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import struct
import sys

paths, target, output = map(Path, sys.argv[1:])
expected = {'uname_test_runsc_systrap', 'kcov_test_runsc_systrap'}
records = {}
for line in paths.read_text().splitlines():
    source = Path(line)
    if not line.startswith('bazel-out/') or source.name not in expected or source.name in records:
        raise ValueError(line)
    data = source.read_bytes()
    assert data.startswith(b'#!/bin/bash')
    commands = [shlex.split(row) for row in data.decode().splitlines() if row.startswith('exec ')]
    assert len(commands) == 1 and commands[0][1].startswith('test/runner/')
    assert commands[0][-1].startswith('test/syscalls/linux/')
    assert not any('..' in Path(name).parts for name in [commands[0][1], commands[0][-1]])
    runfiles = Path(str(source) + '.runfiles')
    assert runfiles.is_dir()
    shutil.copy2(source, target/source.name)
    destination = target/(source.name + '.runfiles')
    shutil.copytree(runfiles, destination, symlinks=False)
    workspace = destination/'_main'
    assert (workspace/commands[0][1]).is_file() and (workspace/commands[0][-1]).is_file()
    for executable in (commands[0][1], commands[0][-1]):
        header = (workspace/executable).read_bytes()[:20]
        assert header[:6] == b'\x7fELF\x02\x01' and struct.unpack_from('<H',header,18)[0] == 62, executable
    records[source.name] = {'sha256': hashlib.sha256(data).hexdigest(), 'source':line, 'runner':commands[0][1], 'test':commands[0][-1], 'runfiles':str(destination), 'workspace':'_main'}
assert set(records) == expected
output.write_text(json.dumps(records,indent=2)+'\n')
