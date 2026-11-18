"""Check task release traversal for the rootful sandbox's mapped host nobody."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

release, public_root, output = map(Path, sys.argv[1:])
assert not release.is_symlink() and not public_root.is_symlink()
assert release.resolve().is_relative_to(public_root.resolve())
assert public_root.stat().st_uid == os.getuid() != 0
assert stat.S_IMODE(public_root.stat().st_mode) == 0o711
report = {'mappedHostUID': 65534, 'mappedHostGID': 65534,
          'release': str(release), 'publicRoot': str(public_root), 'ancestors': [], 'executables': []}
for path in [release, *release.parents]:
    info = path.stat()
    report['ancestors'].append({'path': str(path), 'mode': oct(stat.S_IMODE(info.st_mode)), 'uid': info.st_uid, 'gid': info.st_gid})
for path in sorted(release.rglob('*')):
    assert not path.is_symlink()
    if not path.is_file() or not path.stat().st_mode & 0o111:
        continue
    result = subprocess.run(['sudo', '-u', '#65534', '-g', '#65534', '--', 'test', '-x', str(path)],
                            capture_output=True, text=True, timeout=10)
    report['executables'].append({'path': str(path.relative_to(release)), 'accessExit': result.returncode})
    output.write_text(json.dumps(report, indent=2) + '\n')
    assert result.returncode == 0, (path, result.stderr)
assert report['executables']
report['traversalPassed'] = True
output.write_text(json.dumps(report, indent=2) + '\n')
