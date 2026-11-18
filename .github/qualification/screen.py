"""Execute eight canonical checkpoint capability observations after builds stop."""

import json
import os
import re
import stat
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import TypedDict, cast

from process_group import exists, stop as stop_group
from validate import validate
from private_store import verify_config, verify_info

work, results, support, public = map(Path, sys.argv[1:5])
phase = sys.argv[5] if len(sys.argv) == 6 else 'all'
assert phase in ('base-first', 'remaining', 'all')
active = public / 'active'
runtime_name = 'checkpoint-cost'
records = json.loads((results / 'observations.json').read_text()) if phase == 'remaining' else []
if phase == 'remaining':
    assert len(records) == 1 and records[0]['label'] == 'base-CheckpointEmpty'
    assert records[0]['exit'] == 0 and records[0]['completionMetadata']['atCompletionBeforeCleanup'] is True
child = None
observer = None
active_groups = {}
publishing_child = False
pending_signal = None
terminating = False
marker = work / 'screen-active-groups.json'


def publish_groups() -> None:
    if active_groups or publishing_child:
        marker.write_text(json.dumps({'groups': {role: process.pid if process is not None else None for role, process in active_groups.items()}, 'publishing': publishing_child}) + '\n')
    else:
        marker.unlink(missing_ok=True)


def release(role: str, grace: int = 1) -> None:
    process = active_groups[role]
    assert process is not None, 'Child ownership publication incomplete'
    process.poll()
    stop_group(process.pid, term_seconds=grace, kill_seconds=1, reap=process.poll)
    process.wait(timeout=1)
    assert not exists(process.pid), 'Owned child group remains'
    del active_groups[role]
    publish_groups()


def stop_children() -> bool:
    for role, grace in (('command', 1), ('benchmark', 3), ('observer', 1)):
        if role not in active_groups:
            continue
        try:
            release(role, grace)
        except (AssertionError, OSError, subprocess.SubprocessError) as error:
            raise AssertionError('Owned screen group stop unproved') from error
    return not active_groups


def interrupted(number, _frame) -> None:
    global pending_signal, terminating
    if publishing_child:
        if pending_signal is None:
            pending_signal = number
        return
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    terminating = True
    stop_children()
    raise SystemExit(128 + number)


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)


def start_child(role: str, args: list[str], **kwargs) -> subprocess.Popen:
    global publishing_child, pending_signal
    assert role not in active_groups
    publishing_child = True
    try:
        active_groups[role] = None
        publish_groups()
        process = subprocess.Popen(args, start_new_session=True, **kwargs)
        active_groups[role] = process
        publish_groups()
    except BaseException:
        try:
            if active_groups.get(role) is not None:
                release(role)
        finally:
            publishing_child = False
        raise
    publishing_child = False
    publish_groups()
    if pending_signal is not None:
        number = pending_signal
        pending_signal = None
        interrupted(number, None)
    return process



# Image metadata (including Size) was retained by run.sh; filesystem/memory
# availability is observational, not a fabricated resource sufficiency bound.
(results / (phase + '-worker-memory-before.txt')).write_text(Path('/proc/meminfo').read_text())
disk = shutil.disk_usage(work)
(results / (phase + '-worker-disk-before.json')).write_text(json.dumps({
    'totalBytes': disk.total, 'usedBytes': disk.used, 'freeBytes': disk.free,
}) + '\n')


def command(*args, timeout=30):
    start = time.monotonic()
    process = start_child('command', args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        release('command')
        raise
    release('command')
    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    with (results / 'screen-commands.jsonl').open('a') as output:
        output.write(json.dumps({'argv': args, 'exit': result.returncode,
                                 'seconds': time.monotonic() - start,
                                 'stdout': result.stdout, 'stderr': result.stderr}) + '\n')
    result.check_returncode()
    return result.stdout


def empty():
    assert not command('sudo', 'docker', 'ps', '-aq').split()


def verify(label, side=None):
    private = json.loads((work / 'docker-private-store.json').read_text())['config']
    verify_config(json.loads(command('sudo', 'cat', '/etc/docker/daemon.json')), private)
    verify_info(json.loads(command('sudo', 'docker', 'info', '--format', '{{json .}}')), private)
    command('python3', str(support / 'fileset.py'), 'verify', str(work / 'harness'),
            str(results / 'frozen-harness.json'), str(results / (label + '-harness.json')), timeout=45)
    images = command('sudo', 'docker', 'image', 'inspect', '--format', '{{.Id}}',
                     'gvisor.dev/images/benchmarks/alpine:latest',
                     'gvisor.dev/images/benchmarks/nginx:latest')
    assert images.strip() == (results / 'frozen-images.txt').read_text().strip()
    if side is not None:
        command('python3', str(support / 'fileset.py'), 'verify', str(active),
                str(results / (side + '-runtime-manifest.json')),
                str(results / (label + '-runtime.json')), timeout=45)
        command('sudo', 'python3', str(support / 'quiescent.py'), str(active), timeout=15)
    empty()


class CheckpointCompletion(TypedDict):
    benchmark: str
    containerID: str
    checkpoint: str
    fileCount: int
    logicalBytes: int
    atCompletionBeforeCleanup: bool


def completion_metadata(output: Path, leaf: str, started_ns: int) -> CheckpointCompletion:
    candidates = [path for path in output.iterdir() if path.name.startswith('checkpoint-completion-')]
    assert len(candidates) == 1, [path.name for path in candidates]
    path = candidates[0]
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0
    assert stat.S_IMODE(metadata.st_mode) == 0o600 and 0 < metadata.st_size <= 4096
    assert metadata.st_mtime_ns >= started_ns
    assert re.fullmatch(r'checkpoint-completion-[a-f0-9]{64}-[a-z0-9-]{1,64}[.]json', path.name)
    # The benchmark runs as root. Read only this bounded, allowlisted JSON
    # after its entire process group has stopped, never private payloads.
    raw = command('sudo', 'head', '--bytes=4097', str(path), timeout=5)
    assert len(raw.encode()) <= 4096
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            assert key not in value, 'Duplicate completion metadata key'
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique_object)
    assert isinstance(value, dict) and set(value) == {'benchmark', 'containerID', 'checkpoint', 'fileCount', 'logicalBytes', 'atCompletionBeforeCleanup'}
    assert value['benchmark'] == 'Benchmark' + leaf
    identifier = value['containerID']
    assert isinstance(identifier, str) and re.fullmatch(r'[a-f0-9]{64}', identifier)
    checkpoint = {'CheckpointEmpty': 'ckpt-empty-0', 'RestoreEmpty': 'ckpt-restore-empty-0',
                  'CheckpointNginx': 'ckpt-nginx-0', 'RestoreNginx': 'ckpt-restore-nginx-0'}[leaf]
    assert value['checkpoint'] == checkpoint
    assert path.name == 'checkpoint-completion-' + identifier + '-' + checkpoint + '.json'
    assert type(value['fileCount']) is int and 0 < value['fileCount'] <= 1 << 16
    assert type(value['logicalBytes']) is int and 0 < value['logicalBytes'] <= 2 << 30
    assert value['atCompletionBeforeCleanup'] is True
    owned = work / 'checkpoint-owned-ids.json'
    identifiers = set(json.loads(owned.read_text())) if owned.exists() else set()
    assert all(isinstance(item, str) and re.fullmatch(r'[a-f0-9]{64}', item) for item in identifiers)
    identifiers.add(identifier)
    owned.write_text(json.dumps(sorted(identifiers)) + '\n')
    (results / ('completion-' + leaf + '-' + identifier + '.json')).write_text(raw)
    return cast(CheckpointCompletion, value)


def observe(label, leaf, runtime, side):
    global child, observer
    binary = 'startup_test'
    expected = 'Benchmark' + leaf + '-4'
    selector = '^Benchmark' + leaf + '$'
    verify(label + '-before', side)
    runfiles = work / 'harness' / (binary + '.runfiles')
    temporary = work / 'bench-tmp'
    temporary.mkdir(exist_ok=True)
    output = work / (label + '-private-output')
    output.mkdir()
    args = ['sudo', 'env', 'RUNTIME=' + runtime, 'GOMAXPROCS=4',
            'RUNFILES_DIR=' + str(runfiles), 'TEST_SRCDIR=' + str(runfiles),
            'TEST_WORKSPACE=_main', 'TMPDIR=' + str(temporary), 'TEST_TMPDIR=' + str(temporary),
            'TEST_UNDECLARED_OUTPUTS_DIR=' + str(output),
            'GVISOR_CHECKPOINT_METADATA_ROOT=' + json.loads((results / 'docker-checkpoint-prerequisites.json').read_text())['dockerRootDir'],
            str(work / 'harness' / binary),
            '-runtime=' + runtime, '-test.run=NoTests', '-test.bench=' + selector,
            '-test.benchtime=1x', '-test.count=1', '-test.cpu=4', '-test.timeout=55s', '-test-checkpoint=true']
    (results / (label + '-argv.json')).write_text(json.dumps(args, indent=2) + '\n')
    observer = start_child('observer', ['sudo', 'python3', str(support / 'volume.py'), 'watch',
                                 str(work), str(results), label])
    ready = work / (label + '-volume-ready')
    ready_deadline = time.monotonic() + 8
    while not ready.exists() and observer.poll() is None and time.monotonic() < ready_deadline:
        time.sleep(0.05)
    if not ready.exists():
        interrupted(signal.SIGTERM, None)
    start = time.monotonic()
    with (results / (label + '.log')).open('w') as stream:
        started_ns = time.time_ns()
        child = start_child('benchmark', args, stdout=stream, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        while child.poll() is None and observer.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if child.poll() is None:
            release('benchmark', grace=3)
            status = 124
        else:
            status = child.returncode
            release('benchmark', grace=3)
        (work / (label + '-volume-stop')).write_text('owner stop\n')
        try:
            observer_status = observer.wait(timeout=6)
        except subprocess.TimeoutExpired:
            observer_status = 124
        release('observer')
        observer = None
        result = subprocess.CompletedProcess(args, status)
        child = None
    record = {'label': label, 'leaf': leaf, 'runtime': runtime, 'side': side,
              'exit': result.returncode, 'wallSeconds': time.monotonic() - start,
              'observerExit': observer_status, 'scope': 'single N1 capability/cost with perturbative metadata volume monitoring'}
    records.append(record)
    (results / 'observations.json').write_text(json.dumps(records, indent=2) + '\n')
    # Command failure is sticky; cleanup retains logs. No downstream source
    # observations are attempted after a prerequisite or earlier case fails.
    result.check_returncode()
    assert observer_status == 0, record
    record.update(validate((results / (label + '.log')).read_text(), expected))
    record['completionMetadata'] = completion_metadata(output, leaf, started_ns)
    record['checkpointSizeScope'] = 'Logical bytes at completion before cleanup; not peak size'
    record['observerSampledNonempty'] = json.loads((results / (label + '-volume-complete.json')).read_text())['observedNonemptyCheckpoint']
    verify(label + '-after', side)
    (results / 'observations.json').write_text(json.dumps(records, indent=2) + '\n')



empty()
# docker_setup.py already captured the original config before enabling experimental.
assert (work / 'docker-config-state').is_file()
for side in (('base',) if phase == 'base-first' else ('base', 'candidate')):
    empty()
    command('sudo', 'python3', str(support / 'quiescent.py'), str(active), timeout=15)
    if active.exists():
        shutil.rmtree(active)
    shutil.copytree(work / side / 'runtime', active)
    command('python3', str(support / 'runtime_access.py'), str(active), str(public),
            str(results / (phase + '-' + side + '-actor-access.json')), timeout=30)
    args = ['--platform=systrap', '--network=sandbox', '--debug=false']
    command('sudo', str(active / 'runsc'), 'install', '--config_file=/etc/docker/daemon.json',
            '--runtime=' + runtime_name, '--clobber=true', '--download-sidecars=NEVER',
            '--require-sidecars=NEVER', '--', *args)
    private = json.loads((work / 'docker-private-store.json').read_text())['config']
    verify_config(json.loads(command('sudo', 'cat', '/etc/docker/daemon.json')), private)
    command('sudo', 'systemctl', 'restart', 'docker', timeout=30)
    readiness = results / (phase + '-' + side + '-runtime-readiness.json')
    command('sudo', 'python3', str(support / 'docker_runtime_ready.py'), str(readiness),
            '/etc/docker/daemon.json', runtime_name + '=' + str(active / 'runsc'), timeout=80)
    actual = json.loads(command('sudo', 'cat', '/etc/docker/daemon.json'))['runtimes'][runtime_name]
    daemon = json.loads(readiness.read_text())['observations'][-1]['taskRuntimes'][runtime_name]
    assert actual['runtimeArgs'] == daemon['runtimeArgs'] == args
    command('sudo', 'docker', 'run', '--rm', '--name', 'checkpoint-preflight',
            '--runtime=' + runtime_name, '--network=none',
            'gvisor.dev/images/benchmarks/alpine:latest', '/bin/true', timeout=30)
    for leaf in ('CheckpointEmpty', 'RestoreEmpty', 'CheckpointNginx', 'RestoreNginx'):
        if phase == 'base-first' and leaf != 'CheckpointEmpty':
            continue
        if phase == 'remaining' and side == 'base' and leaf == 'CheckpointEmpty':
            continue
        observe(side + '-' + leaf, leaf, runtime_name, side)
if phase == 'base-first':
    assert len(records) == 1 and records[0]['label'] == 'base-CheckpointEmpty'
    (results / 'first-base-completion-passed.json').write_text(json.dumps(records[0], indent=2) + '\n')
    raise SystemExit(0)
assert len(records) == 8
assert {record['label'] for record in records} == {side + '-' + leaf for side in ('base', 'candidate') for leaf in ('CheckpointEmpty', 'RestoreEmpty', 'CheckpointNginx', 'RestoreNginx')}
(results / 'screen-complete.json').write_text(json.dumps({
    'observations': 8, 'statisticalQualification': False, 'runcBenchmarkControl': False,
    'source': os.environ['SOURCE'], 'base': os.environ['BASE'], 'records': records,
    'runtimeBase': '1f33fe3003455db178b97f3b43d3a012f1611994',
    'runtimeCandidate': 'd2c5c08fa3753bcf14ea6015fad547ee8f3d531c',
    'firstBaselineBeforeCandidateBuild': True,
    'privateCheckpointPayloadsArchived': False,
}, indent=2) + '\n')
