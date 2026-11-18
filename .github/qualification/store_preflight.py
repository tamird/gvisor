"""Prove actual task namespace placement using one exactly owned native probe."""

import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from private_store import configuration, probe_owned, verify_config, verify_info

work, results = map(Path, sys.argv[1:])
state = json.loads((work / 'docker-private-store.json').read_text())
private = configuration(work, state['config']['containerd-namespace'])
assert state['externalContainerd'] == '/run/containerd/containerd.sock'
marker_path = work / 'store-preflight-owned.json'
image = json.loads((results / 'image-alpine.json').read_text())[0]['Id']
assert re.fullmatch(r'sha256:[a-f0-9]{64}', image)
marker = {'name': 'checkpoint-cost-store-' + uuid.uuid4().hex, 'image': image, 'runtime': 'runc'}

def write_marker(value: dict[str, str]) -> None:
    pending = marker_path.with_suffix('.pending')
    pending.write_text(json.dumps(value, indent=2) + '\n')
    pending.replace(marker_path)

deadline = time.monotonic() + 18
calls = []


def command(*args: str) -> str:
    remaining = deadline - time.monotonic()
    assert remaining > 0, 'Private namespace probe deadline exhausted'
    result = subprocess.run(args, capture_output=True, text=True, timeout=min(4, remaining))
    calls.append({'argv': args, 'exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
    (results / 'store-preflight-commands.json').write_text(json.dumps(calls, indent=2) + '\n')
    result.check_returncode()
    return result.stdout


verify_config(json.loads(Path('/etc/docker/daemon.json').read_text()), private)
verify_info(json.loads(command('docker', 'info', '--format', '{{json .}}')), private)
assert not command('docker', 'ps', '-aq').split()
assert command('docker', 'image', 'inspect', '--format', '{{.Id}}', 'gvisor.dev/images/benchmarks/alpine:latest').strip() == image
# A durable exact name and immutable metadata precede the create API. The outer
# cleanup can recover a created container even if its CLI receipt is lost.
write_marker(marker)
primary = None
cleanup_error = None
try:
    identifier = command('docker', 'create', '--name', marker['name'], '--runtime=runc', '--network=none', image, '/bin/sleep', '20').strip()
    assert re.fullmatch(r'[a-f0-9]{64}', identifier)
    marker['id'] = identifier
    write_marker(marker)
    info = json.loads(command('docker', 'inspect', identifier))[0]
    assert probe_owned(info, marker) == identifier
    command('docker', 'start', identifier)
    info = json.loads(command('docker', 'inspect', identifier))[0]
    assert probe_owned(info, marker) == identifier and info['State']['Running'] is True
    argv = ['ctr', '--address', state['externalContainerd'], '--namespace', private['containerd-namespace'], 'containers', 'info', identifier]
    raw = command(*argv)
    # containerd v2.4.2 ctr prints core/containers.Container.ID, as uppercase ID.
    actual = json.loads(raw)
    assert actual['ID'] == identifier, (actual.get('ID'), identifier)
    (results / 'store-preflight-containerd-info.json').write_text(raw)
    (results / 'store-preflight-identity.json').write_text(json.dumps({'docker': info, 'containerdID': actual['ID'], 'containerdSocket': state['externalContainerd'], 'containerdNamespace': private['containerd-namespace'], 'privateRoot': private['data-root'], 'scope': 'native dependency prerequisite only'}, indent=2) + '\n')
except Exception as error:
    primary = error
finally:
    deadline = time.monotonic() + 8
    # Do not conceal a failed probe with another operation. If this bounded
    # cleanup fails, the durable marker is retained for outer exact-ID cleanup.
    try:
        if 'id' in marker:
            info = json.loads(command('docker', 'inspect', marker['id']))[0]
            probe_owned(info, marker)
            command('docker', 'rm', '-f', marker['id'])
            assert marker['id'] not in command('docker', 'ps', '-aq', '--no-trunc').split()
            marker_path.unlink()
    except Exception as error:
        cleanup_error = str(error)
        if primary is None:
            primary = error
if primary is not None:
    (results / 'store-preflight-failure.json').write_text(json.dumps({'primaryType': type(primary).__name__, 'primary': str(primary), 'cleanup': cleanup_error}, indent=2) + '\n')
    raise primary
