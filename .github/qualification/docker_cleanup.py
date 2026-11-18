"""Restore the task's Docker configuration and remove only its containers."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from owned_commands import Commands
from container_identity import TEMPLATE, owned

scratch, results = map(Path, sys.argv[1:])
state = scratch / 'docker-config-state'
native_started = (scratch / 'native-started').exists()
commands = Commands(results / 'cleanup-active-group.json')
if not state.exists() and not native_started:
    raise SystemExit(0)
errors = []

def command(*args: str, timeout: float = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 0, commands.command(list(args), timeout=timeout), '')


def wait_inventory(expected: dict, label: str) -> None:
    deadline = time.monotonic() + 10
    observations = []
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, 'Original Docker runtime inventory was not restored'
        actual = json.loads(command('sudo', 'docker', 'info', '--format', '{{json .Runtimes}}',
                                    timeout=min(5, remaining)).stdout)
        observations.append(actual)
        (results / (label + '-runtime-inventory.json')).write_text(json.dumps({
            'expected': expected, 'observations': observations, 'ready': actual == expected,
        }, indent=2) + '\n')
        if actual == expected:
            return
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))

try:
    intents = [json.loads(path.read_text()) for path in sorted((results / 'invocation-intents').glob('*.json'))
               if not (results / 'invocation-complete' / path.stem).exists()]
    ids = command('sudo', 'docker', 'ps', '-aq', '--no-trunc').stdout.split()
    actual = [json.loads(command('sudo', 'docker', 'inspect', '--format', TEMPLATE, identifier).stdout) for identifier in ids]
    selected = owned(actual, intents, os.environ['SOURCE'])
    (results / 'cleanup-owned-containers.json').write_text(json.dumps(selected, indent=2) + '\n')
    for value in selected:
        current = json.loads(command('sudo', 'docker', 'inspect', '--format', TEMPLATE, value['id']).stdout)
        assert current == value, 'Owned identity changed before removal'
        command('sudo', 'docker', 'rm', '-f', value['id'])
    assert not command('sudo', 'docker', 'ps', '-aq').stdout.split(), 'Containers remain'
except (AssertionError, KeyError, ValueError, subprocess.SubprocessError, OSError) as error:
    (results / 'docker-restoration.json').write_text(json.dumps({
        'errors': [str(error)], 'restored': False,
        'reason': 'Container ownership or quiescence unproved; original configuration retained for diagnosis',
    }, indent=2) + '\n')
    raise SystemExit(1)
original = restored = None
try:
    if state.exists():
        expected = json.loads((results / 'runtimes-before.json').read_text())
        present = state.read_text().strip() == 'present'
        configuration = json.loads(command('sudo', 'cat', str(scratch / 'docker-original.json')).stdout) if present else {}
        assert isinstance(configuration, dict), 'Original Docker configuration is not an object'
        restored_runtimes = configuration.get('runtimes', {})
        assert restored_runtimes is None or isinstance(restored_runtimes, dict), 'Original runtimes are not an object'
        staged = {**configuration, 'runtimes': restored_runtimes or {}}
        proposed = scratch / 'docker-restore-runtimes.json'
        proposed.write_text(json.dumps(staged, indent=2) + '\n')
        (results / 'restoration-runtime-map.json').write_text(json.dumps({
            'originalRuntimesKeyPresent': 'runtimes' in configuration,
            'explicitOriginalMapAppliedBeforeByteRestoration': True,
        }, indent=2) + '\n')
        # Moby reload retains the current map when the key is absent. First
        # apply the explicit original map, then put the original bytes back.
        try:
            command('sudo', 'cp', str(proposed), '/etc/docker/daemon.json')
            command('sudo', 'systemctl', 'reload', 'docker')
            wait_inventory(expected, 'explicit-map')
        except (AssertionError, ValueError, OSError, subprocess.SubprocessError) as error:
            errors.append('Explicit original runtime map: ' + str(error))
        if present:
            command('sudo', 'cp', '-a', str(scratch / 'docker-original.json'), '/etc/docker/daemon.json')
        else:
            command('sudo', 'rm', '-f', '/etc/docker/daemon.json')
        command('sudo', 'systemctl', 'reload', 'docker')
        wait_inventory(expected, 'original-bytes')
    if not state.exists():
        pass
    elif state.read_text().strip() == 'present':
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
except (AssertionError, ValueError, KeyError, subprocess.SubprocessError, OSError) as error:
    errors.append(str(error))
    original = restored = None
try:
    command('sudo', 'chown', '-hR', f'{os.getuid()}:{os.getgid()}', str(results))
except (subprocess.SubprocessError, OSError) as error:
    errors.append(str(error))
(results / 'docker-restoration.json').write_text(json.dumps({
    'errors': errors, 'originalSHA256': original, 'restoredSHA256': restored,
    'restored': not errors,
}, indent=2) + '\n')
if errors:
    raise SystemExit('\n'.join(errors))
