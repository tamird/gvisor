"""Record actual source-built release ELF identity without executing a runtime."""
import json
from pathlib import Path
import struct
import sys

release, output = map(Path, sys.argv[1:])
assert (release / 'runsc').is_file() and (release / 'gvisor-bin/gvisor_sentry').is_file()
records = []
for path in sorted(release.rglob('*')):
    assert not path.is_symlink()
    if not path.is_file():
        continue
    with path.open('rb') as stream:
        header = stream.read(64)
    if header[:4] != b'\x7fELF':
        continue
    assert len(header) == 64 and header[4] == 2 and header[5] == 1
    machine = struct.unpack('<H', header[18:20])[0]
    assert machine == 62, (path, machine)
    records.append({'path': str(path.relative_to(release)), 'class': 64, 'machine': machine})
assert {'runsc', 'gvisor-bin/gvisor_sentry'} <= {item['path'] for item in records}
output.write_text(json.dumps({'architecture': 'amd64', 'sourceBuiltCanonicalRelease': True, 'ELFs': records}, indent=2) + '\n')
