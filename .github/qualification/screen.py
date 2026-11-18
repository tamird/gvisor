"""Execute the fixed missing-only capability screen after every build is over."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from validate import validate
from owned_commands import Commands
from container_identity import intent, complete

phase = sys.argv[1]
assert phase in ("native", "smoke", "sources")
work, results, support, public = map(Path, sys.argv[2:])
active = public / 'active'
runtime_name = 'vfio-gofer-pilot'
records = json.loads((results / 'observations.json').read_text()) if phase in ('smoke', 'sources') else []
if phase in ('smoke', 'sources'):
    assert len(records) == 4 and (results / 'native-gates-complete.json').is_file()
commands = Commands(results / 'screen-active-groups.json')
# Image metadata (including Size) was retained by run.sh; filesystem/memory
# availability is observational, not a fabricated resource sufficiency bound.
(results / ('worker-memory-before-' + phase + '.txt')).write_text(Path('/proc/meminfo').read_text())
disk = shutil.disk_usage(work)
(results / ('worker-disk-before-' + phase + '.json')).write_text(json.dumps({
    'totalBytes': disk.total, 'usedBytes': disk.used, 'freeBytes': disk.free,
}) + '\n')


def command(*args, timeout=30, grace=1):
    start = time.monotonic()
    try:
        stdout = commands.command(list(args), timeout=timeout, grace=grace)
    except subprocess.CalledProcessError as error:
        with (results / 'screen-commands.jsonl').open('a') as output:
            output.write(json.dumps({'argv': args, 'exit': error.returncode,
                                     'seconds': time.monotonic() - start,
                                     'stdout': error.stdout, 'stderr': error.stderr}) + '\n')
        raise
    with (results / 'screen-commands.jsonl').open('a') as output:
        output.write(json.dumps({'argv': args, 'exit': 0,
                                 'seconds': time.monotonic() - start,
                                 'stdout': stdout}) + '\n')
    return stdout


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
    intent(results, label, os.environ['SOURCE'],
           json.loads((results / ('image-alpine.json' if startup else 'image-rubydev.json')).read_text())[0]['Id'],
           runtime, (r'/BenchmarkStartupEmpty-[A-Z2-7]{32}' if startup else
                     r'/BenchmarkRubyNoOpTest-page_cache.dirty-filesystem[.]' + filesystem + r'-[A-Z2-7]{32}'),
           time.time())
    start = time.monotonic()
    with (results / (label + '.log')).open('w') as stream:
        try:
            child = commands.start('benchmark', args, stdout=stream, stderr=subprocess.STDOUT)
            try:
                status = child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                status = 124
        finally:
            if 'benchmark' in commands.active:
                commands.release('benchmark', grace=3)
        result = subprocess.CompletedProcess(args, status)
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
    complete(results, label)
    (results / 'observations.json').write_text(json.dumps(records, indent=2) + '\n')


# Native FUSE readiness and workload are required before either runtime build.
if phase == 'native':
    (work / 'native-started').write_text('four fresh source-bound native Ruby gates\n')
    for filesystem in ('fusefs', 'tmpfs', 'rootfs', 'bindfs'):
        observe('native-' + filesystem, filesystem, 'runc')
    (results / 'native-gates-complete.json').write_text(json.dumps({'observations':4,'actualFUSEGatePassed':True,'comparativeTimingControl':False})+'\n')
    raise SystemExit(0)

empty()
assert not Path('/etc/docker/daemon.json').is_symlink()
if not (work / 'docker-config-state').exists():
    configuration_exists = subprocess.run(['sudo', 'test', '-e', '/etc/docker/daemon.json'], timeout=10)
    assert configuration_exists.returncode in (0, 1)
    if configuration_exists.returncode == 0:
        command('sudo', 'test', '-f', '/etc/docker/daemon.json')
        command('sudo', 'cp', '-a', '/etc/docker/daemon.json', str(work / 'docker-original.json'))
        (work / 'docker-config-state').write_text('present\n')
    else:
        (work / 'docker-config-state').write_text('absent\n')

def activate(side, directfs):
    mode = str(directfs).lower()
    activation = phase + '-' + mode + '-' + side
    empty()
    command('sudo', 'python3', str(support / 'quiescent.py'), str(active), timeout=15)
    if active.exists():
        shutil.rmtree(active)
    shutil.copytree(work / side / 'runtime', active)
    command('python3', str(support / 'runtime_access.py'), str(active), str(public),
            str(results / (activation + '-actor-access.json')), timeout=30)
    # One fixed active path; changing mode is explicit and independently read back.
    args = ['--platform=systrap', '--network=sandbox', '--debug=false', '--directfs=' + mode]
    command('sudo', str(active / 'runsc'), 'install', '--config_file=/etc/docker/daemon.json',
            '--runtime=' + runtime_name, '--clobber=true', '--download-sidecars=NEVER',
            '--require-sidecars=NEVER', '--', *args)
    command('sudo', 'systemctl', 'reload', 'docker', timeout=30)
    readiness = results / (activation + '-runtime-readiness.json')
    command('sudo', 'python3', str(support / 'docker_runtime_ready.py'), str(readiness),
            '/etc/docker/daemon.json', runtime_name + '=' + str(active / 'runsc'), timeout=80)
    actual = json.loads(command('sudo', 'cat', '/etc/docker/daemon.json'))['runtimes'][runtime_name]
    daemon = json.loads(readiness.read_text())['observations'][-1]['taskRuntimes'][runtime_name]
    assert actual['runtimeArgs'] == daemon['runtimeArgs'] == args
    intent(results, activation + '-preflight', os.environ['SOURCE'],
           json.loads((results / 'image-alpine.json').read_text())[0]['Id'],
           runtime_name, r'/vfio-gofer-preflight', time.time())
    command('sudo', 'docker', 'run', '--rm', '--name', 'vfio-gofer-preflight',
            '--runtime=' + runtime_name, '--network=none',
            'gvisor.dev/images/benchmarks/alpine:latest', '/bin/true', timeout=30)
    empty()
    complete(results, activation + '-preflight')

if phase == 'smoke':
    for side in ('base', 'candidate'):
        activate(side, True)
        verify('owner-' + side + '-before', side)
        command('python3', str(support / 'smoke.py'), str(work), str(results), str(support), side,
                runtime_name, timeout=180, grace=12)
        verify('owner-' + side + '-after', side)
    raise SystemExit(0)

for owner in ('native', 'base', 'candidate'):
    assert (results / ('owner-' + owner) / 'verified.json').is_file()

for directfs in (False, True):
    mode = str(directfs).lower()
    for side in ('base', 'candidate'):
        activate(side, directfs)
        for filesystem in (('bindfs', 'tmpfs', 'rootfs', 'fusefs') if directfs else ('tmpfs', 'rootfs', 'fusefs')):
            observe(mode + '-' + side + '-' + filesystem, filesystem, runtime_name, side)
        if directfs:
            observe(mode + '-' + side + '-startup', 'startup', runtime_name, side)

assert len(records) == 20
(results / 'screen-complete.json').write_text(json.dumps({
    'observations': 20, 'newNativeRubyControls': 4, 'reusedNativeRubyControls': 0, 'sourceRubyLeaves': 14,
    'sourceStartupLeaves': 2, 'statisticalQualification': False,
    'fullFilesystemCategoryQualified': False, 'harnessSource': os.environ['SOURCE'],
    'runtimeBase':'1f33fe3003455db178b97f3b43d3a012f1611994',
    'runtimeCandidate':'1b37f9cfca65287f8c153314e392d4fbf7c4094b', 'records': records,
}, indent=2) + '\n')
