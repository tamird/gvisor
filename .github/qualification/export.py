"""Verify that canonical Go export retains the repaired TCP source bytes."""

import hashlib
import json
import sys
import zipfile
from pathlib import Path

records = []
with zipfile.ZipFile('bazel-bin/gopath.zip') as archive:
    for name in ('connect.go', 'endpoint.go', 'rcv.go'):
        path = Path('pkg/tcpip/transport/tcp', name)
        exported = archive.read('src/gvisor.dev/gvisor/' + str(path))
        if exported != path.read_bytes():
            raise SystemExit(f'Canonical Go export changed source bytes: {path}')
        records.append({'path': str(path), 'sha256': hashlib.sha256(exported).hexdigest()})
Path(sys.argv[1], 'go-export-provenance.json').write_text(json.dumps(records, indent=2) + '\n')
