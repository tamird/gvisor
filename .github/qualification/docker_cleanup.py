"""Restore the task's Docker configuration and remove only its containers."""

import hashlib
import json
import os
import re
import subprocess
import signal
import time
from typing import NotRequired, TypedDict

from process_group import exists, stop
from private_store import inventory, probe_owned, verify_config
import sys
from pathlib import Path

scratch, results = map(Path, sys.argv[1:])
state = scratch / 'docker-config-state'
native_started = (scratch / 'native-started').exists()
if not state.exists() and not native_started:
    raise SystemExit(0)
errors = []
private_path = scratch / 'docker-private-store.json'
private_state = json.loads(private_path.read_text()) if private_path.exists() else None

marker = scratch / 'cleanup-active-group.json'
active_child = None
publishing_child = False
pending_signal = None

def stop_active() -> None:
    global active_child
    if active_child is None:
        return
    process = active_child
    process.poll()
    stop(process.pid, term_seconds=1, kill_seconds=1, reap=process.poll)
    process.wait(timeout=1)
    assert not exists(process.pid), 'Cleanup child group remains'
    marker.unlink(missing_ok=True)
    active_child = None


def interrupted(number, _frame) -> None:
    global pending_signal
    if publishing_child:
        if pending_signal is None:
            pending_signal = number
        return
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    stop_active()
    (results / 'cleanup-interrupted.json').write_text(json.dumps({'signal': number, 'ownedGroupQuiescent': True}) + '\n')
    raise SystemExit(128 + number)


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)


def start_command(args: tuple[str, ...]) -> subprocess.Popen:
    global active_child, publishing_child, pending_signal
    publishing_child = True
    try:
        marker.write_text(json.dumps({'pgid': None}) + '\n')
        active_child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        marker.write_text(json.dumps({'pgid': active_child.pid}) + '\n')
    except BaseException:
        try:
            stop_active()
        finally:
            publishing_child = False
        raise
    publishing_child = False
    if pending_signal is not None:
        number = pending_signal
        pending_signal = None
        interrupted(number, None)
    return active_child



deadline = time.monotonic() + 190

def command(*args: str, timeout=20) -> subprocess.CompletedProcess[str]:
    remaining = deadline - time.monotonic() - 5
    if remaining <= 0:
        raise TimeoutError('Docker restoration deadline exhausted')
    process = start_command(args)
    try:
        stdout, stderr = process.communicate(timeout=min(timeout, remaining))
    except subprocess.TimeoutExpired:
        stop_active()
        raise
    stop_active()
    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    with (results / 'docker-restoration-commands.jsonl').open('a') as stream:
        stream.write(json.dumps({'argv': args, 'exit': result.returncode,
                                 'stdout': stdout, 'stderr': stderr}) + '\n')
    result.check_returncode()
    return result

class NamespaceResource(TypedDict):
    exit: int | None
    stdout: str | None
    stderr: str | None
    error: NotRequired[str]


class NamespaceReport(TypedDict):
    address: str
    namespace: str
    phase: str
    resources: dict[str, NamespaceResource]


def capture_namespace(address: str, namespace: str, phase: str) -> NamespaceReport:
    # Snapshot/ingest CLIs have no quiet output in retained containerd 2.4.2.
    listings = {
        'containers': ('containers', 'list', '--quiet'),
        'tasks': ('tasks', 'list', '--quiet'),
        'images': ('images', 'list', '--quiet'),
        'snapshots': ('snapshots', 'list'),
        'ingests': ('content', 'active'),
        'leases': ('leases', 'list', '--quiet'),
        'content': ('content', 'list', '--quiet'),
    }
    report: NamespaceReport = {'address': address, 'namespace': namespace, 'phase': phase, 'resources': {}}
    for resource, args in listings.items():
        try:
            result = command('sudo', 'ctr', '--address', address, '--namespace', namespace,
                             *args, timeout=2)
            report['resources'][resource] = {'exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
        except (subprocess.SubprocessError, OSError) as error:
            stdout, stderr = getattr(error, 'stdout', None), getattr(error, 'stderr', None)
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors='replace')
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors='replace')
            report['resources'][resource] = {'error': str(error), 'exit': getattr(error, 'returncode', None),
                                             'stdout': stdout, 'stderr': stderr}
    (results / ('namespace-' + namespace + '-' + phase + '.json')).write_text(json.dumps(report, indent=2) + '\n')
    return report


def remove_task_namespace(address: str, namespace: str) -> None:
    report = capture_namespace(address, namespace, 'before-removal')
    resources = report['resources']
    if any(value['exit'] != 0 or not isinstance(value['stdout'], str) for value in resources.values()):
        raise ValueError('Task namespace inventory failed; metadata retained')
    if any(resources[name]['stdout'].strip() for name in ('containers', 'tasks', 'images')):
        raise ValueError('Task namespace still has live resources; metadata retained')
    # Match only the exact empty headers observed by the native API probe.
    # Nonempty rows and unknown schemas are rejected without parsing tables.
    if resources['snapshots']['stdout'].split() != ['KEY', 'PARENT', 'KIND']:
        raise ValueError('Task namespace has snapshots or an unknown listing')
    if resources['ingests']['stdout'].split() != ['REF', 'SIZE', 'AGE']:
        raise ValueError('Task namespace has active ingests or an unknown listing')
    leases = resources['leases']['stdout'].splitlines()
    content = resources['content']['stdout'].splitlines()
    if len(leases) != len(set(leases)) or any(not re.fullmatch(r'[A-Za-z0-9_.:][A-Za-z0-9_.:-]{0,255}', item) for item in leases):
        raise ValueError('Unexpected task lease identifier; metadata retained')
    if len(content) != len(set(content)) or any(not re.fullmatch(r'sha256:[a-f0-9]{64}', item) for item in content):
        raise ValueError('Unexpected task content digest; metadata retained')
    for identifier in leases:
        command('sudo', 'ctr', '--address', address, '--namespace', namespace,
                'leases', 'delete', identifier, timeout=2)
    # Releasing a lease may let ordinary containerd GC remove an owned ref.
    # Never trigger GC or infer successful deletion from the CLI exit alone.
    current = command('sudo', 'ctr', '--address', address, '--namespace', namespace,
                      'content', 'list', '--quiet', timeout=2).stdout.splitlines()
    if not set(current) <= set(content):
        raise ValueError('Task namespace content changed during cleanup')
    absent_after_error = []
    for digest in current:
        try:
            command('sudo', 'ctr', '--address', address, '--namespace', namespace,
                    'content', 'delete', digest, timeout=2)
        except subprocess.CalledProcessError:
            remaining = command('sudo', 'ctr', '--address', address, '--namespace', namespace,
                                'content', 'list', '--quiet', timeout=2).stdout.splitlines()
            if digest in remaining or not set(remaining) <= set(content):
                raise
            absent_after_error.append(digest)
    after = capture_namespace(address, namespace, 'after-metadata-deletion')
    for resource in ('containers', 'tasks', 'images', 'leases', 'content'):
        value = after['resources'][resource]
        if value['exit'] != 0 or value['stdout'].strip():
            raise ValueError('Task namespace resources remain after metadata deletion')
    for resource, header in (('snapshots', ['KEY', 'PARENT', 'KIND']), ('ingests', ['REF', 'SIZE', 'AGE'])):
        value = after['resources'][resource]
        if value['exit'] != 0 or value['stdout'].split() != header:
            raise ValueError('Task namespace snapshot/ingest state changed')
    (results / ('namespace-' + namespace + '-metadata-deletion.json')).write_text(json.dumps({
        'namespace': namespace, 'originalLeases': leases, 'originalContentRefs': content,
        'refsAbsentAfterNonzeroDelete': absent_after_error, 'globalGCRequested': False,
    }, indent=2) + '\n')
    command('sudo', 'ctr', '--address', address, 'namespaces', 'remove', namespace, timeout=2)


# The stage owner must have reaped the screen parent before rollback.
assert not (results / 'stage-active-group.json').exists(), 'Refuse rollback while stage ownership remains'
# A screen that failed normally may retain detached role groups. Stop every
# recorded group before touching Docker/packages; malformed publication fails closed.
screen_marker = scratch / 'screen-active-groups.json'
if screen_marker.exists():
    groups = json.loads(screen_marker.read_text())
    assert groups['publishing'] is False
    assert isinstance(groups['groups'], dict)
    for role, identifier in groups['groups'].items():
        assert role in ('command', 'benchmark', 'observer')
        assert isinstance(identifier, int) and identifier > 1
        stop(identifier, term_seconds=1, kill_seconds=1)
        assert not exists(identifier), 'Refuse rollback while screen group remains'
    screen_marker.unlink()

# Never race a setup package helper when its Python/apt leader already exited.
active_group = scratch / 'docker-active-group.json'
if active_group.exists():
    identifier = json.loads(active_group.read_text())['pgid']
    assert isinstance(identifier, int) and identifier > 1
    stop(identifier)
    assert not exists(identifier), 'Refuse rollback while setup group remains'
    active_group.unlink()

if private_state is None and not native_started and not (scratch / 'docker-packages-mutated').exists():
    # Preparation failed before any daemon/store/package mutation. Do not
    # restart an unrelated preexisting daemon merely because it was snapshotted.
    command('sudo', 'chown', '-hR', f'{os.getuid()}:{os.getgid()}', str(results))
    (results / 'docker-restoration.json').write_text(json.dumps({'errors': [], 'restored': True, 'dockerNotMutated': True, 'originalDaemonNotRestarted': True}) + '\n')
    raise SystemExit(0)

try:
    ids = command('sudo', 'docker', 'ps', '-aq', '--no-trunc').stdout.split()
    owned_path = scratch / 'checkpoint-owned-ids.json'
    known_ids = set(json.loads(owned_path.read_text())) if owned_path.exists() else set()
    for container in ids:
        info = json.loads(command('sudo', 'docker', 'inspect', container).stdout)[0]
        runtime, name = info['HostConfig']['Runtime'], info['Name']
        marker_file = scratch / 'store-preflight-owned.json'
        probe = json.loads(marker_file.read_text()) if marker_file.exists() else None
        store_probe = False
        if probe is not None and name == '/' + probe['name']:
            try:
                probe_owned(info, probe)
                store_probe = True
                (results / 'store-preflight-cleanup.json').write_text(json.dumps({'actualID': info['Id'], 'ownedName': probe['name'], 'image': info['Image'], 'runtime': runtime, 'idReceiptWasMissing': 'id' not in probe}, indent=2) + '\n')
            except AssertionError as error:
                errors.append('Store probe ownership mismatch: ' + str(error))
                continue
        if store_probe:
            # This native prerequisite never creates checkpoint data.
            command('sudo', 'docker', 'rm', '-f', container)
            continue
        native = (native_started and runtime == 'runc' and name == '/checkpoint-cost-image-input')
        if (state.exists() and runtime == 'checkpoint-cost') or native:
            known_ids.add(container)
            owned_path.write_text(json.dumps(sorted(known_ids)) + '\n')
            command('sudo', 'docker', 'rm', '-f', container)
except (subprocess.SubprocessError, OSError, ValueError) as error:
    errors.append(str(error))
original = restored = None
try:
    owned_path = scratch / 'checkpoint-owned-ids.json'
    if owned_path.exists():
        root = Path(json.loads((results / 'docker-checkpoint-prerequisites.json').read_text())['dockerRootDir'])
        owned = json.loads(owned_path.read_text())
        for identifier in owned:
            if not re.fullmatch(r'[a-f0-9]{64}', identifier):
                raise ValueError('Invalid owned checkpoint container identifier')
            path = root / 'containers' / identifier / 'checkpoints'
            result = subprocess.run(['sudo', 'test', '-e', str(path)], timeout=5)
            if result.returncode != 1:
                raise ValueError('Task checkpoint directory was not deleted by container cleanup')
        (results / 'checkpoint-deletion.json').write_text(json.dumps({'ownedIDs': owned, 'allCheckpointDirectoriesAbsent': True}) + '\n')
except (subprocess.SubprocessError, OSError, ValueError) as error:
    errors.append(str(error))
try:
    if private_state is not None:
        command('sudo', 'systemctl', 'stop', 'docker.service', 'docker.socket', timeout=20)
        if command('systemctl', 'show', '--property=MainPID', '--value', 'docker.service').stdout.strip() != '0':
            raise ValueError('Task daemon did not stop before package restoration')
        prepared = scratch / 'docker-private.json'
        if hashlib.sha256(prepared.read_bytes()).hexdigest() != private_state['configSHA256']:
            raise ValueError('Prepared private-store config identity changed')
        # Package hooks may restart Docker. Keep every such start on the task
        # store until the original daemon package versions are verified.
        command('sudo', 'install', '-m', '0644', str(prepared), '/etc/docker/daemon.json')
        verify_config(json.loads(Path('/etc/docker/daemon.json').read_text()), private_state['config'])
    if (scratch / 'docker-packages-mutated').exists():
        restored_packages = json.loads((scratch / 'docker-original-packages.json').read_text())
        for filename, digest in restored_packages['files'].items():
            if command('sudo', 'sha256sum', filename).stdout.split()[0] != digest:
                raise ValueError('Original Docker package identity changed')
        command('sudo', 'apt-get', '--yes', '--no-install-recommends', '--allow-downgrades',
                'install', *sorted(restored_packages['files']), timeout=90)
        for name, version in restored_packages['versions'].items():
            actual = command('dpkg-query', '-W', '-f=${Version}', name).stdout.strip()
            if actual != version:
                raise ValueError('Original Docker package version not restored')
    retained_containerd = results / 'retained-containerd-version.txt'
    if retained_containerd.exists() and command('dpkg-query', '-W', '-f=${Version}', 'containerd.io').stdout.strip() != retained_containerd.read_text().strip():
        raise ValueError('Retained containerd package version changed')
    # Only the successfully verified original package versions may reattach
    # the original store. Any earlier error leaves the task config in place.
    if state.exists():
        command('sudo', 'systemctl', 'stop', 'docker.service', 'docker.socket')
        if command('systemctl', 'show', '--property=MainPID', '--value', 'docker.service').stdout.strip() != '0':
            raise ValueError('Restored-package daemon did not stop before original-store reattachment')
        if private_state is not None:
            address = private_state['externalContainerd']
            if address:
                verify_config(json.loads(Path('/etc/docker/daemon.json').read_text()), private_state['config'])
                current = command('sudo', 'ctr', '--address', address, 'namespaces', 'list', '--quiet').stdout.split()
                task_namespaces = {private_state['config'][key] for key in ('containerd-namespace', 'containerd-plugins-namespace')}
                originals = set(private_state['originalNamespaces'])
                if not originals <= set(current) or not set(current) <= originals | task_namespaces:
                    raise ValueError('Containerd namespace ownership/inventory changed')
                for key in ('containerd-namespace', 'containerd-plugins-namespace'):
                    namespace = private_state['config'][key]
                    if namespace in private_state['originalNamespaces']:
                        raise ValueError('Task namespace overlaps original containerd state')
                    if namespace in current:
                        remove_task_namespace(address, namespace)
                remaining = command('sudo', 'ctr', '--address', address, 'namespaces', 'list', '--quiet').stdout.split()
                if set(remaining) != set(private_state['originalNamespaces']):
                    raise ValueError('Containerd namespace inventory was not restored')
        if state.read_text().strip() == 'present':
            command('sudo', 'cp', '-a', str(scratch / 'docker-original.json'), '/etc/docker/daemon.json')
        else:
            command('sudo', 'rm', '-f', '/etc/docker/daemon.json')
        command('sudo', 'systemctl', 'restart', 'docker')
        version = json.loads(command('sudo', 'docker', 'version', '--format', '{{json .}}').stdout)
        (results / 'docker-restored-version.json').write_text(json.dumps(version, indent=2) + '\n')
        original_version = json.loads((results / 'docker-original-version.json').read_text())
        if version['Server']['Version'] != original_version['Server']['Version']:
            raise ValueError('Original Docker daemon version not restored')
        restored_inventory = inventory(lambda *args: command('sudo', *args).stdout)
        original_inventory = json.loads((results / 'docker-original-inventory.json').read_text())
        if restored_inventory != original_inventory:
            raise ValueError('Original Docker store/image/volume inventory not restored')
        if private_state is not None:
            wanted_socket = private_state['originalSocketState']
            command('sudo', 'systemctl', 'start' if wanted_socket == 'active' else 'stop', 'docker.socket')
            if command('systemctl', 'show', '--property=ActiveState', '--value', 'docker.socket').stdout.strip() != wanted_socket:
                raise ValueError('Original Docker socket state was not restored')
            if command('systemctl', 'show', '--property=ActiveState', '--value', 'docker.service').stdout.strip() != 'active':
                raise ValueError('Original Docker service is not active after socket restoration')
        (results / 'docker-restored-inventory.json').write_text(json.dumps(restored_inventory, indent=2) + '\n')
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
except (subprocess.SubprocessError, OSError, ValueError) as error:
    errors.append(str(error))
    original = restored = None
try:
    command('sudo', 'chown', '-hR', f'{os.getuid()}:{os.getgid()}', str(results))
except (subprocess.SubprocessError, OSError, ValueError) as error:
    errors.append(str(error))
(results / 'docker-restoration.json').write_text(json.dumps({
    'errors': errors, 'originalSHA256': original, 'restoredSHA256': restored,
    'restored': not errors,
}, indent=2) + '\n')
if errors:
    raise SystemExit('\n'.join(errors))
