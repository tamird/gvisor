"""Observe only task checkpoint metadata; never read or retain memory payloads."""

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

from private_store import verify_info

mode, work_arg, results_arg, *extra = sys.argv[1:]
work, results = Path(work_arg), Path(results_arg)
prefixes = ('BenchmarkCheckpointEmpty-', 'BenchmarkRestoreEmpty-',
            'BenchmarkCheckpointNginx-', 'BenchmarkRestoreNginx-')


# CLI children deliberately inherit the observer group. The screen owns and
# proves absence of that entire group, including descendants of an exited CLI.
def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, timeout=5)


def owner(info):
    assert re.fullmatch(r'[a-f0-9]{64}', info['Id'])
    assert info['Name'].lstrip('/').startswith(prefixes), info['Name']
    assert info['HostConfig']['Runtime'] == 'checkpoint-cost'
    image = 'nginx' if label.endswith('Nginx') else 'alpine'
    assert info['Image'] == json.loads((results / ('image-' + image + '.json')).read_text())[0]['Id']
    return info['Id']


def private(path):
    assert path.is_absolute() and path.resolve() == path
    existing = [item for item in [path, *path.parents] if item.exists()]
    assert not any(item.is_symlink() for item in existing)
    assert any(stat.S_IMODE(item.stat().st_mode) & 0o007 == 0 for item in existing), path


info = json.loads(docker('info', '--format', '{{json .}}'))
verify_info(info, json.loads((work / 'docker-private-store.json').read_text())['config'])
root = Path(info['DockerRootDir'])
assert root.is_absolute() and root.resolve() == root
assert not docker('ps', '-aq').split() if mode == 'preflight' else True
private(root / 'containers')
if mode == 'preflight':
    version = json.loads(docker('version', '--format', '{{json .}}'))
    assert 17 <= int(version['Server']['Version'].split('.')[0]) <= 27
    assert version['Server']['Experimental'] is True
    (results / 'docker-checkpoint-prerequisites.json').write_text(json.dumps({
        'version': version, 'dockerRootDir': str(root), 'privateAncestryVerified': True,
        'sampledCheckpointStopBytes': 2 * 1024**3, 'minimumFreeBytes': 8 * 1024**3,
        'hardPeakOrQuotaBound': False,
    }, indent=2) + '\n')
    raise SystemExit(0)

assert mode == 'watch' and os.getuid() == 0
label = extra[0]
assert re.fullmatch(r'(?:base|candidate)-(?:CheckpointEmpty|RestoreEmpty|CheckpointNginx|RestoreNginx)', label)
owned = work / 'checkpoint-owned-ids.json'
ids = set(json.loads(owned.read_text())) if owned.exists() else set()
(work / (label + '-volume-ready')).write_text('ready\n')
stop = work / (label + '-volume-stop')
samples = results / (label + '-volume.jsonl')
start = time.monotonic()
observed_checkpoint_bytes = False
while not stop.exists() and time.monotonic() - start < 80:
    current = docker('ps', '-aq').split()
    for identifier in current:
        assert re.fullmatch(r'[a-f0-9]{12,64}', identifier)
        try:
            inspected = json.loads(docker('inspect', identifier))[0]
        except subprocess.CalledProcessError:
            # Canonical cleanup can remove a short-lived container between
            # discovery and inspect. Absence is not a fabricated size sample.
            assert identifier not in docker('ps', '-aq').split()
            continue
        ids.add(owner(inspected))
    owned.write_text(json.dumps(sorted(ids)) + '\n')
    owner_metadata = work.stat()
    os.chown(owned, owner_metadata.st_uid, owner_metadata.st_gid)
    total = count = 0
    directories = []
    races = []
    for identifier in sorted(ids):
        checkpoint = root / 'containers' / identifier / 'checkpoints'
        if not checkpoint.exists():
            continue
        try:
            private(checkpoint)
            directories.append(str(checkpoint))
            def walk_error(error):
                raise error
            for parent, folders, files in os.walk(checkpoint, followlinks=False, onerror=walk_error):
                for name in folders + files:
                    path = Path(parent) / name
                    metadata = path.lstat()
                    assert not stat.S_ISLNK(metadata.st_mode), path
                    assert stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode), path
                    if stat.S_ISREG(metadata.st_mode):
                        total += metadata.st_size
                        count += 1
        except FileNotFoundError as error:
            # Canonical cleanup may remove only this already-owned checkpoint
            # between enumeration and stat. A partial traversal is not size0.
            assert error.filename and Path(error.filename).is_relative_to(root / 'containers' / identifier), error
            races.append({'checkpoint': str(checkpoint), 'missingPath': str(error.filename)})
    filesystem = os.statvfs(root)
    free = filesystem.f_bavail * filesystem.f_frsize
    threshold = total > 2 * 1024**3 or free < 8 * 1024**3
    observed_checkpoint_bytes |= not races and total > 0
    with samples.open('a') as stream:
        stream.write(json.dumps({'seconds': time.monotonic() - start, 'ownedIDs': sorted(ids),
                                 'checkpointDirectories': directories, 'fileCount': count,
                                 'logicalBytes': None if races else total, 'partialLowerBoundBytes': total,
                                 'metadataAvailable': not races, 'disappearanceRaces': races, 'availableDiskBytes': free,
                                 'stopThresholdExceeded': threshold, 'sampledNotHardPeak': True}) + '\n')
    if threshold:
        raise SystemExit('Sampled checkpoint/disk stop threshold exceeded')
    time.sleep(0.2)
assert stop.exists(), 'Volume observer reached its bound without owner stop'
(results / (label + '-volume-complete.json')).write_text(json.dumps({
    'observedNonemptyCheckpoint': observed_checkpoint_bytes,
    'sampledNotHardPeak': True, 'ownedIDs': sorted(ids),
}) + '\n')
# Completion metadata from the owning benchmark separately proves nonempty
# checkpoint contents; polling may miss a short-lived completed checkpoint.
