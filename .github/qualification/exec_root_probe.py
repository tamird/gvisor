"""Prove public task-root ancestry and actual mapped-actor execution before builds."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

root, output = map(Path, sys.argv[1:3])
prefix = sys.argv[3]
assert sys.platform == 'linux'
assert not root.is_symlink() and root.resolve().parent == Path('/tmp').resolve()
assert re.fullmatch(re.escape(prefix) + r'[A-Za-z0-9]{6}', root.name)
assert root.stat().st_uid == os.getuid() != 0
assert stat.S_IMODE(root.stat().st_mode) == 0o711
probe = root / 'public-host-execution-probe'
report = {'mappedHostUID': 65534, 'mappedHostGID': 65534,
          'scope': 'public host /usr/bin/true traversal/noexec preflight, not a project workload',
          'root': str(root), 'ancestors': []}
for path in [root, *root.parents]:
    info = path.stat()
    report['ancestors'].append({'path': str(path), 'mode': oct(stat.S_IMODE(info.st_mode)), 'uid': info.st_uid, 'gid': info.st_gid})
output.write_text(json.dumps(report, indent=2) + '\n')
assert not probe.exists()
try:
    shutil.copyfile(Path('/usr/bin/true').resolve(), probe)
    probe.chmod(0o555)
    for flag, expected in [('-u', '65534'), ('-g', '65534')]:
        actual = subprocess.run(['sudo', '-u', '#65534', '-g', '#65534', '--', 'id', flag],
                                capture_output=True, text=True, timeout=10, check=True)
        report['actualUID' if flag == '-u' else 'actualGID'] = int(actual.stdout.strip())
        output.write_text(json.dumps(report, indent=2) + '\n')
        assert actual.stdout.strip() == expected
    executed = subprocess.run(['sudo', '-u', '#65534', '-g', '#65534', '--', str(probe)],
                              capture_output=True, text=True, timeout=10)
    report['actualExecutionExit'] = executed.returncode
    report['actualExecutionStderr'] = executed.stderr
    output.write_text(json.dumps(report, indent=2) + '\n')
    assert executed.returncode == 0
    report['passed'] = True
finally:
    probe.unlink(missing_ok=True)
    report['publicHostProbeRemoved'] = not probe.exists()
    output.write_text(json.dumps(report, indent=2) + '\n')
