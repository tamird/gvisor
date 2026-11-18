"""Probe canonical native FUSE device availability, then one exact device control."""

import json
import os
from pathlib import Path
import stat
import signal
import subprocess
import sys

mode, work_arg, results_arg, support_arg = sys.argv[1:]
work, results, support = map(Path, (work_arg, results_arg, support_arg))
owned_file = work / 'owned.json'
owned = json.loads(owned_file.read_text()) if owned_file.exists() else []
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


def command(*args, timeout=10):
    global child
    child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=1)
        raise
    value = subprocess.CompletedProcess(args, child.returncode, stdout, stderr)
    child = None
    return {'argv': args, 'exit': value.returncode, 'stdout': value.stdout, 'stderr': value.stderr}


def required(*args, timeout=10):
    value = command(*args, timeout=timeout)
    assert value['exit'] == 0, value
    return value['stdout']


if mode == 'cleanup':
    for identifier in owned:
        inspect = command('sudo', 'docker', 'inspect', identifier)
        if inspect['exit'] != 0:
            continue
        info = json.loads(inspect['stdout'])[0]
        assert info['Name'].startswith('/vfio-fuse-prerequisite-')
        assert info['Image'] == json.loads((support / 'image.json').read_text())['id']
        required('sudo', 'docker', 'rm', '-f', identifier)
    assert not required('sudo', 'docker', 'ps', '-aq').split()
    (results / 'cleanup.json').write_text(json.dumps({'owned': owned, 'allAbsent': True}) + '\n')
    raise SystemExit(0)

assert mode == 'run'
assert not required('sudo', 'docker', 'ps', '-aq').split()
expected = json.loads((support / 'image.json').read_text())
required('sudo', 'docker', 'pull', expected['tag'], timeout=240)
image = json.loads(required('sudo', 'docker', 'image', 'inspect', expected['tag']))[0]
assert image['Id'] == expected['id'] and image['Architecture'] == 'amd64'
(results / 'image.json').write_text(json.dumps(image, indent=2) + '\n')
host = os.stat('/dev/fuse')
assert stat.S_ISCHR(host.st_mode)
assert (os.major(host.st_rdev), os.minor(host.st_rdev)) == (10, 229)
report = {'source': os.environ['SOURCE'], 'base': os.environ['BASE'],
          'hostDevice': {'path': '/dev/fuse', 'major': 10, 'minor': 229,
                         'mode': stat.S_IMODE(host.st_mode)},
          'scope': 'Prerequisite diagnosis only; no FUSE mount/workload or capability result', 'cases': []}
for control in (False, True):
    label = 'device-control' if control else 'canonical-old-config'
    args = ['sudo', 'docker', 'create', '--name', 'vfio-fuse-prerequisite-' + label,
            '--runtime=runc', '--mount=type=tmpfs,destination=/data',
            '--mount=type=tmpfs,destination=/fuse']
    if control:
        args += ['--device=/dev/fuse:/dev/fuse:rwm']
    args += [expected['id'], 'sleep', '24h']
    identifier = required(*args).strip()
    owned.append(identifier)
    owned_file.write_text(json.dumps(owned) + '\n')
    required('sudo', 'docker', 'start', identifier)
    info = json.loads(required('sudo', 'docker', 'inspect', identifier))[0]
    assert info['Image'] == expected['id'] and info['HostConfig']['Runtime'] == 'runc'
    assert info['HostConfig']['Privileged'] is False
    devices = info['HostConfig'].get('Devices') or []
    assert devices == ([{'PathOnHost': '/dev/fuse', 'PathInContainer': '/dev/fuse', 'CgroupPermissions': 'rwm'}] if control else [])
    # Exec privilege is the existing canonical FUSE helper's ExecOpts.Privileged;
    # this does not make the container privileged or add any other host device.
    script = 'id; for f in /dev/fuse /data /fuse; do stat -c "%n %F %a %t:%T" "$f"; done; ruby -e \'begin; File.open("/dev/fuse", "r+") { |f| puts "FUSE_OPEN_OK" }; rescue => e; warn "FUSE_OPEN_ERROR #{e.class}: #{e.message}"; exit 11; end\''
    probe = command('sudo', 'docker', 'exec', '--privileged', identifier, 'sh', '-c', script)
    report['cases'].append({'label': label, 'createArgv': args, 'inspect': info, 'probe': probe,
                            'openSucceeded': 'FUSE_OPEN_OK' in probe['stdout'] and probe['exit'] == 0,
                            'openResultObserved': 'FUSE_OPEN_OK' in probe['stdout'] or 'FUSE_OPEN_ERROR' in probe['stderr']})
    (results / 'diagnosis.json').write_text(json.dumps(report, indent=2) + '\n')
    required('sudo', 'docker', 'rm', '-f', identifier)
    owned.remove(identifier)
    owned_file.write_text(json.dumps(owned) + '\n')
assert len(report['cases']) == 2
report['captureComplete'] = all(case['openResultObserved'] for case in report['cases'])
report['fuseMountOrBenchmarkCapabilityClaimed'] = False
(results / 'diagnosis.json').write_text(json.dumps(report, indent=2) + '\n')
assert report['captureComplete'], 'Open-result evidence missing; retain both actual outcomes'
