"""Restore the task's Docker configuration and remove only its containers."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

scratch, results = map(Path, sys.argv[1:])
state = scratch / 'docker-config-state'
if not state.exists():
    raise SystemExit(0)
errors = []

def command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=20, check=True)

try:
    ids = command('sudo', 'docker', 'ps', '-aq').stdout.split()
    for container in ids:
        runtime = command('sudo', 'docker', 'inspect', '--format', '{{.HostConfig.Runtime}}', container).stdout.strip()
        if runtime == 'systrap':
            command('sudo', 'docker', 'rm', '-f', container)
except (subprocess.SubprocessError, OSError) as error:
    errors.append(str(error))
try:
    if state.read_text().strip() == 'present':
        command('sudo', 'cp', '-a', str(scratch / 'docker-original.json'), '/etc/docker/daemon.json')
    else:
        command('sudo', 'rm', '-f', '/etc/docker/daemon.json')
    command('sudo', 'systemctl', 'restart', 'docker')
    if state.read_text().strip() == 'present':
        original = command('sudo', 'sha256sum', str(scratch / 'docker-original.json')).stdout.split()[0]
        restored = command('sudo', 'sha256sum', '/etc/docker/daemon.json').stdout.split()[0]
        if original != restored:
            errors.append('Docker daemon configuration bytes were not restored')
        old_metadata = command('sudo', 'stat', '-c', '%a:%u:%g', str(scratch / 'docker-original.json')).stdout.strip()
        restored_metadata = command('sudo', 'stat', '-c', '%a:%u:%g', '/etc/docker/daemon.json').stdout.strip()
        if old_metadata != restored_metadata:
            errors.append('Docker daemon configuration mode/owner was not restored')
    else:
        original = restored = None
        if Path('/etc/docker/daemon.json').exists():
            errors.append('Docker daemon configuration should be absent after restoration')
except (subprocess.SubprocessError, OSError) as error:
    errors.append(str(error))
    original = restored = None
try:
    command('sudo', 'chown', '-R', f'{os.getuid()}:{os.getgid()}', str(results))
except (subprocess.SubprocessError, OSError) as error:
    errors.append(str(error))
(results / 'docker-restoration.json').write_text(json.dumps({
    'errors': errors, 'originalSHA256': original, 'restoredSHA256': restored,
    'restored': not errors,
}, indent=2) + '\n')
if errors:
    raise SystemExit('\n'.join(errors))
