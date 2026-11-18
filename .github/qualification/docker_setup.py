"""Prepare a pinned supported disposable-worker Docker; preserve restoration inputs."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import signal

import sys

from process_group import exists, stop
from rollback_packages import acquire_originals
from private_store import configuration, external_containerd, inventory, verify_config, verify_info, verify_unit

work, results, support = map(Path, sys.argv[1:])
assert os.getuid() == 0


active_child = None
publishing_child = False
pending_signal = None


def stop_active():
    global active_child
    if active_child is None:
        return
    process = active_child
    # Reap the leader first where possible; its status never substitutes for
    # checking the whole owned group, which may still contain dpkg descendants.
    process.poll()
    stop(process.pid, reap=process.poll)
    process.wait(timeout=1)
    assert not exists(process.pid), 'Owned setup group still exists'
    (work / 'docker-active-group.json').unlink(missing_ok=True)
    active_child = None


def interrupted(number, frame):
    global pending_signal
    if publishing_child:
        if pending_signal is None:
            pending_signal = number
        return
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    stop_active()
    (results / 'docker-setup-interrupted.json').write_text(json.dumps({'signal': number, 'ownedProcessGroupQuiescentBeforeRollback': True}) + '\n')
    raise SystemExit(128 + number)


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)


def start_command(args, cwd=None):
    global active_child, publishing_child, pending_signal
    publishing_child = True
    try:
        (work / 'docker-active-group.json').write_text(json.dumps({'pgid': None}) + '\n')
        active_child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, cwd=cwd, start_new_session=True)
        (work / 'docker-active-group.json').write_text(json.dumps({'pgid': active_child.pid}) + '\n')
    except BaseException:
        try:
            stop_active()
            (work / 'docker-active-group.json').unlink(missing_ok=True)
        finally:
            publishing_child = False
        raise
    publishing_child = False
    if pending_signal is not None:
        number = pending_signal
        pending_signal = None
        interrupted(number, None)
    return active_child


def command(*args, timeout=30, cwd=None):
    process = start_command(args, cwd)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_active()
        raise
    stop_active()
    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    with (results / 'docker-dependency-commands.jsonl').open('a') as stream:
        stream.write(json.dumps({'argv': args, 'exit': result.returncode,
                                 'stdout': result.stdout, 'stderr': result.stderr}) + '\n')
    result.check_returncode()
    return result.stdout


assert not command('docker', 'ps', '-aq').split()
original = json.loads(command('docker', 'version', '--format', '{{json .}}'))
(results / 'docker-original-version.json').write_text(json.dumps(original, indent=2) + '\n')
config = Path('/etc/docker/daemon.json')
assert not config.is_symlink()
assert not (work / 'docker-config-state').exists(), 'Do not overwrite original restoration state'
if config.exists():
    command('cp', '-a', str(config), str(work / 'docker-original.json'))
    (work / 'docker-config-state').write_text('present\n')
else:
    (work / 'docker-config-state').write_text('absent\n')
original_inventory = inventory(command)
(results / 'docker-original-inventory.json').write_text(json.dumps(original_inventory, indent=2) + '\n')
unit = command('systemctl', 'show', '--property=ExecStart', '--value', 'docker.service')
verify_unit(unit)
original_config = json.loads(config.read_text()) if config.exists() else {}
assert isinstance(original_config, dict), 'Original daemon config is not an object'
address = external_containerd(unit, original_config)
assert command('systemctl', 'show', '--property=ActiveState', '--value', 'docker.service').strip() == 'active'
socket_state = command('systemctl', 'show', '--property=ActiveState', '--value', 'docker.socket').strip()
assert socket_state in ('active', 'inactive'), socket_state
namespace = 'checkpoint-cost-' + str(os.getpid())
private = configuration(work, namespace)
private_config = work / 'docker-private.json'
private_config.write_text(json.dumps(private) + '\n')
if address:
    (results / 'containerd-client-server-version.txt').write_text(command('ctr', '--address', address, 'version'))
    namespaces = command('ctr', '--address', address, 'namespaces', 'list', '--quiet').split()
    assert namespace not in namespaces and namespace + '-plugins' not in namespaces
else:
    namespaces = []
validator = 'dockerd'
pinned = []
if int(original['Server']['Version'].split('.')[0]) > 27:
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    assert release['VERSION_CODENAME'].strip('"') == 'noble'
    assert command('dpkg', '--print-architecture').strip() == 'amd64'
    containerd = command('dpkg-query', '-W', '-f=${Version}', 'containerd.io').strip()
    (results / 'retained-containerd-version.txt').write_text(containerd + '\n')
    packages = work / 'docker-packages'
    packages.mkdir(mode=0o700)
    original_dir = packages / 'original'
    original_dir.mkdir(mode=0o700)
    originals = {name: command('dpkg-query', '-W', '-f=${Version}', name).strip()
                 for name in ('docker-ce', 'docker-ce-cli')}
    package_metadata = json.loads((support / 'docker-pins.json').read_text())
    files = acquire_originals(command, original_dir, originals, package_metadata['rollbackPackages'])
    (work / 'docker-original-packages.json').write_text(json.dumps({
        'versions': originals, 'files': files,
    }, indent=2) + '\n')
    pins = json.loads((support / 'docker-pins.json').read_text())['packages']
    pinned = []
    for item in pins:
        path = packages / Path(item['Filename']).name
        command('curl', '--proto', '=https', '-fsSL', '--max-time', '120',
                'https://download.docker.com/linux/ubuntu/' + item['Filename'], '-o', str(path), timeout=125)
        assert path.stat().st_size == int(item['Size'])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['SHA256']
        pinned.append(str(path))
    simulated = command('apt-get', '--simulate', '--no-install-recommends', '--allow-downgrades', 'install', *pinned)
    assert not any(line.startswith('Remv ') for line in simulated.splitlines())
    changed = {line.split()[1] for line in simulated.splitlines() if line.startswith('Inst ')}
    assert changed <= {'docker-ce', 'docker-ce-cli'}, changed
    # Inspect/validate the exact pinned daemon and unit before any mutation.
    extracted = work / 'docker-validation'
    command('dpkg-deb', '--extract', next(path for path in pinned if Path(path).name.startswith('docker-ce_')), str(extracted), timeout=30)
    validator = str(extracted / 'usr/bin/dockerd')
    units = [path for path in (extracted / 'lib/systemd/system/docker.service', extracted / 'usr/lib/systemd/system/docker.service') if path.is_file()]
    assert len(units) == 1, units
    verify_unit(units[0].read_text())
    assert external_containerd(units[0].read_text(), {}) == address
command(validator, '--validate', '--config-file=' + str(private_config))
for key in ('data-root', 'exec-root'):
    Path(private[key]).mkdir(mode=0o700)
assert private['data-root'] != original_inventory['root']
# Durable ownership state precedes service/config mutation. The original store
# is never opened by a downgraded daemon, including package post-install starts.
private_state = {'config': private, 'configSHA256': hashlib.sha256(private_config.read_bytes()).hexdigest(), 'externalContainerd': address, 'originalNamespaces': namespaces, 'originalSocketState': socket_state}
(work / 'docker-private-store.json').write_text(json.dumps(private_state, indent=2) + '\n')
command('systemctl', 'stop', 'docker.service', 'docker.socket', timeout=30)
assert command('systemctl', 'show', '--property=MainPID', '--value', 'docker.service').strip() == '0'
command('install', '-m', '0644', str(private_config), str(config))
verify_config(json.loads(config.read_text()), private)
if pinned:
    (work / 'docker-packages-mutated').write_text('restore originals before reattaching original store\n')
    command('apt-get', '--yes', '--no-install-recommends', '--allow-downgrades', 'install', *pinned, timeout=180)
    assert command('dpkg-query', '-W', '-f=${Version}', 'docker-ce').strip() == '5:27.5.1-1~ubuntu.24.04~noble'
    assert command('dpkg-query', '-W', '-f=${Version}', 'containerd.io').strip() == containerd
command('systemctl', 'restart', 'docker', timeout=30)
verify_info(json.loads(command('docker', 'info', '--format', '{{json .}}')), private)
verify_config(json.loads(config.read_text()), private)
assert not command('docker', 'image', 'ls', '-aq').split(), 'New task store must initially be empty'
(results / 'docker-private-store.json').write_text(json.dumps(private_state, indent=2) + '\n')
version = json.loads(command('docker', 'version', '--format', '{{json .}}'))
assert int(version['Server']['Version'].split('.')[0]) <= 27
assert version['Server']['Experimental'] is True
(results / 'docker-active-version.json').write_text(json.dumps(version, indent=2) + '\n')
