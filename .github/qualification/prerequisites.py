"""Record only worker capability/IPC metadata for the native consumer control."""
import json
import os
from pathlib import Path
import sys

mode, results_arg = sys.argv[1:]
results = Path(results_arg)
rows = Path('/proc/sysvipc/sem').read_text().splitlines()
assert rows
if mode == 'before':
    assert len(rows) == 1, 'Exclusive worker has semaphore sets; do not alter unrelated state'
    assert os.getuid() == 0
    fields = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    effective = int(fields['CapEff'].strip(), 16)
    assert effective & (1 << 21) and effective & (1 << 7), 'Native control requires actual SYS_ADMIN and SETUID'
    (results / 'native-host-prerequisites.json').write_text(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'effectiveCapabilities': fields['CapEff'].strip(), 'sysvipcSemaphoreSets': 0, 'scope': 'metadata only; no IPC or capability changes'}, indent=2) + '\n')
else:
    assert mode == 'after'
    (results / 'native-host-ipc-after.json').write_text(json.dumps({'sysvipcSemaphoreSets': len(rows)-1, 'unrelatedIPCDeleted': False}) + '\n')
    assert len(rows) == 1, 'Native consumer left semaphore sets; retain evidence without blanket deletion'
