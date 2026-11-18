"""Execute the fixed missing-only capability screen after every build is over."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from validate import validate

work, results, support, public = map(Path, sys.argv[1:])
active = public / 'active'
runtime_name = 'vfio-gofer-pilot'
records = []
child = None


def interrupted(number, frame):
    if child is not None and child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=1)
    raise SystemExit(128 + number)


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)

# Image metadata (including Size) was retained by run.sh; filesystem/memory
# availability is observational, not a fabricated resource sufficiency bound.
(results / 'worker-memory-before.txt').write_text(Path('/proc/meminfo').read_text())
disk = shutil.disk_usage(work)
(results / 'worker-disk-before.json').write_text(json.dumps({
    'totalBytes': disk.total, 'usedBytes': disk.used, 'freeBytes': disk.free,
}) + '\n')


def command(*args, timeout=30):
    global child
    start = time.monotonic()
    child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=1)
        raise
    result = subprocess.CompletedProcess(args, child.returncode, stdout, stderr)
    child = None
    with (results / 'screen-commands.jsonl').open('a') as output:
        output.write(json.dumps({'argv': args, 'exit': result.returncode,
                                 'seconds': time.monotonic() - start,
                                 'stdout': result.stdout, 'stderr': result.stderr}) + '\n')
    result.check_returncode()
    return result.stdout


def empty():
    assert not command('sudo', 'docker', 'ps', '-aq').split()


def verify(label, side=None):
    command('python3', str(support / 'fileset.py'), 'verify', str(work / 'harness'),
            str(results / 'frozen-harness.json'), str(results / (label + '-harness.json')), timeout=45)
    images = command('sudo', 'docker', 'image', 'inspect', '--format', '{{.Id}}',
                     'gvisor.dev/images/benchmarks/alpine:latest',
                     'gvisor.dev/images/benchmarks/rubydev:latest')
    assert images == (results / 'frozen-images.txt').read_text()
    if side is not None:
        command('python3', str(support / 'fileset.py'), 'verify', str(active),
                str(results / (side + '-runtime-manifest.json')),
                str(results / (label + '-runtime.json')), timeout=45)
        command('sudo', 'python3', str(support / 'quiescent.py'), str(active), timeout=15)
    empty()


def observe(label, filesystem, runtime, side=None):
    global child
    startup = filesystem == 'startup'
    binary = 'startup_test' if startup else 'rubydev_test'
    expected = ('BenchmarkStartupEmpty-4' if startup else
                'BenchmarkRubyNoOpTest/page_cache.dirty/filesystem.' + filesystem + '-4')
    selector = (r'^BenchmarkStartupEmpty$' if startup else
                r'^BenchmarkRubyNoOpTest$/^page_cache[.]dirty$/^filesystem[.]' + filesystem + '$')
    verify(label + '-before', side)
    runfiles = work / 'harness' / (binary + '.runfiles')
    temporary = work / 'bench-tmp'
    temporary.mkdir(exist_ok=True)
    output = results / (label + '-output')
    output.mkdir()
    args = ['sudo', 'env', 'RUNTIME=' + runtime, 'GOMAXPROCS=4',
            'RUNFILES_DIR=' + str(runfiles), 'TEST_SRCDIR=' + str(runfiles),
            'TEST_WORKSPACE=_main', 'TMPDIR=' + str(temporary), 'TEST_TMPDIR=' + str(temporary),
            'TEST_UNDECLARED_OUTPUTS_DIR=' + str(output), str(work / 'harness' / binary),
            '-runtime=' + runtime, '-test.run=NoTests', '-test.bench=' + selector,
            '-test.benchtime=1x', '-test.count=1', '-test.cpu=4', '-test.timeout=25s']
    (results / (label + '-argv.json')).write_text(json.dumps(args, indent=2) + '\n')
    start = time.monotonic()
    with (results / (label + '.log')).open('w') as stream:
        child = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            status = child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=1)
            status = 124
        result = subprocess.CompletedProcess(args, status)
        child = None
    record = {'label': label, 'filesystem': filesystem, 'runtime': runtime, 'side': side,
              'exit': result.returncode, 'wallSeconds': time.monotonic() - start,
              'scope': 'single N1 capability/cost only'}
    records.append(record)
    (results / 'observations.json').write_text(json.dumps(records, indent=2) + '\n')
    # Command failure is sticky; cleanup retains logs. No downstream source
    # observations are attempted when their native/input gate fails.
    result.check_returncode()
    record.update(validate((results / (label + '.log')).read_text(), expected))
    verify(label + '-after', side)
    (results / 'observations.json').write_text(json.dumps(records, indent=2) + '\n')


# Full runfiles already reproduce the prior manifest, including this FUSE server.
fuse = work / 'harness/rubydev_test.runfiles/_main/test/runner/fuse/fuse_/fuse'
assert fuse.is_file() and not fuse.is_symlink() and os.access(fuse, os.X_OK)
assert fuse.read_bytes()[:6] == b'\x7fELF\x02\x01'
(results / 'native-fuse-prerequisites.json').write_text(json.dumps({
    'canonicalFrozenServer': str(fuse),
    'hostDevFuseExists': Path('/dev/fuse').exists(),
    'actualReadinessGate': 'Native canonical dirty-fusefs N1 must pass before either source; no emulation/substitute or readiness relaxation',
}) + '\n')
(work / 'native-started').write_text('missing native mount controls\n')
for filesystem in ('tmpfs', 'rootfs', 'fusefs'):
    observe('native-' + filesystem, filesystem, 'runc')

empty()
assert not Path('/etc/docker/daemon.json').is_symlink()
configuration_exists = subprocess.run(['sudo', 'test', '-e', '/etc/docker/daemon.json'], timeout=10)
assert configuration_exists.returncode in (0, 1)
if configuration_exists.returncode == 0:
    command('sudo', 'cp', '-a', '/etc/docker/daemon.json', str(work / 'docker-original.json'))
    (work / 'docker-config-state').write_text('present\n')
else:
    (work / 'docker-config-state').write_text('absent\n')

for directfs in (False, True):
    mode = str(directfs).lower()
    for side in ('base', 'candidate'):
        empty()
        command('sudo', 'python3', str(support / 'quiescent.py'), str(active), timeout=15)
        if active.exists():
            shutil.rmtree(active)
        shutil.copytree(work / side / 'runtime', active)
        command('python3', str(support / 'runtime_access.py'), str(active), str(public),
                str(results / (mode + '-' + side + '-actor-access.json')), timeout=30)
        # One fixed active path; changing mode is explicit and independently read back.
        args = ['--platform=systrap', '--network=sandbox', '--debug=false', '--directfs=' + mode]
        command('sudo', str(active / 'runsc'), 'install', '--config_file=/etc/docker/daemon.json',
                '--runtime=' + runtime_name, '--clobber=true', '--download-sidecars=NEVER',
                '--require-sidecars=NEVER', '--', *args)
        command('sudo', 'systemctl', 'restart', 'docker', timeout=30)
        readiness = results / (mode + '-' + side + '-runtime-readiness.json')
        command('sudo', 'python3', str(support / 'docker_runtime_ready.py'), str(readiness),
                '/etc/docker/daemon.json', runtime_name + '=' + str(active / 'runsc'), timeout=80)
        actual = json.loads(command('sudo', 'cat', '/etc/docker/daemon.json'))['runtimes'][runtime_name]
        daemon = json.loads(readiness.read_text())['observations'][-1]['taskRuntimes'][runtime_name]
        assert actual['runtimeArgs'] == daemon['runtimeArgs'] == args
        command('sudo', 'docker', 'run', '--rm', '--name', 'vfio-gofer-preflight',
                '--runtime=' + runtime_name, '--network=none',
                'gvisor.dev/images/benchmarks/alpine:latest', '/bin/true', timeout=30)
        for filesystem in (('bindfs', 'tmpfs', 'rootfs', 'fusefs') if directfs else ('tmpfs', 'rootfs', 'fusefs')):
            observe(mode + '-' + side + '-' + filesystem, filesystem, runtime_name, side)
        if directfs:
            observe(mode + '-' + side + '-startup', 'startup', runtime_name, side)

assert len(records) == 19
(results / 'screen-complete.json').write_text(json.dumps({
    'observations': 19, 'nativeNewMountControls': 3, 'sourceRubyLeaves': 14,
    'sourceStartupLeaves': 2, 'statisticalQualification': False,
    'fullFilesystemCategoryQualified': False, 'source': os.environ['SOURCE'],
    'base': os.environ['BASE'], 'records': records,
}, indent=2) + '\n')
