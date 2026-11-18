"""Reject replacement while a process executes a task release binary."""

import hashlib
import sys
from pathlib import Path

release = Path(sys.argv[1])
known = {hashlib.sha256(path.read_bytes()).hexdigest()
         for path in release.rglob('*') if path.is_file()}
live = []
for process in Path('/proc').iterdir():
    if not process.name.isdecimal():
        continue
    executable = process / 'exe'
    try:
        name = str(executable.readlink())
        if 'runsc' not in name and 'gvisor' not in name:
            continue
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        if digest in known:
            live.append(int(process.name))
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
if live:
    raise SystemExit(f'Task release executables still running: {live}')
