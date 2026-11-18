"""Bounded exact-source HTTPD controls; capture completion never clears errors."""

import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time

from subject_metadata import dynamic_evidence, final_subjects, request_accounting_complete

mode = sys.argv[1]
work, results, support = map(Path, sys.argv[2:5])


def command(*args, timeout=20, check=True):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check)


def containers():
    return command('sudo', 'docker', 'ps', '-aq').stdout.split()


def remove_owned():
    path = work / 'owned-containers.json'
    owned = set(json.loads(path.read_text())) if path.exists() else set()
    if (work / 'active-visit').exists():
        owned.update(containers())
    for identifier in sorted(owned):
        command('sudo', 'docker', 'rm', '-f', identifier, check=False)
    path.write_text('[]\n')
    (work / 'active-visit').unlink(missing_ok=True)


if mode == 'cleanup':
    remove_owned()
    subprocess.run(['python3', str(support / 'docker_cleanup.py'), str(work), str(results)], check=True, timeout=40)
    command('sudo', 'systemctl', 'restart', 'docker', timeout=20)
    configured = json.loads(command('sudo', 'docker', 'info', '--format', '{{json .Runtimes}}', timeout=10).stdout)
    original_path = work / 'docker-original.json'
    original = json.loads(command('sudo', 'cat', str(original_path), timeout=5).stdout) if original_path.exists() else {}
    expected_runtime = original.get('runtimes', {}).get('systrap')
    if expected_runtime is None:
        assert 'systrap' not in configured, configured
    else:
        assert configured['systrap']['path'] == expected_runtime['path'], configured
    (results / 'docker-runtime-restoration.json').write_text(json.dumps({'actualRuntimes': configured, 'expectedSystrap': expected_runtime, 'passed': True}, indent=2) + '\n')
    root_record = work / 'public-runtime-root'
    if root_record.exists():
        root = Path(root_record.read_text().strip())
        assert root.parent == Path('/tmp') and re.fullmatch(r'gvisor-httpd-exec\.[A-Za-z0-9]{6}', root.name)
        assert not root.is_symlink() and root.stat().st_uid == os.getuid()
        assert stat.S_IMODE(root.stat().st_mode) == 0o711
        if (root / 'active').exists():
            command('sudo', 'python3', str(support / 'quiescent.py'), str(root / 'active'), timeout=10)
        shutil.rmtree(root)
    raise SystemExit(0)

images = {name: json.loads((results / f'image-{name}.json').read_text())[0]['Id'] for name in ('httpd', 'hey', 'util')}
(results / 'diagnostic-images.json').write_text(json.dumps(images, indent=2) + '\n')
if mode == 'static':
    assert not containers()
    record = {'scope': 'Separate controlled public-image configuration instance before subjects', 'imageID': images['httpd'], 'host': {}}
    for path in ('/proc/sys/net/core/somaxconn', '/proc/sys/net/ipv4/tcp_max_syn_backlog', '/proc/meminfo', '/proc/self/status'):
        record['host'][path] = Path(path).read_text()
    script = 'set -e; mkdir -p /tmp/html; cp -r /local/* /tmp/html/.; apache2 -V; apache2 -X -t -D DUMP_RUN_CFG; for f in /etc/apache2/apache2.conf /etc/apache2/ports.conf /etc/apache2/mods-enabled/mpm*.conf; do echo "FILE:$f"; cat "$f"; done'
    args = ['sudo', 'docker', 'create', '--name', 'gvisor-httpd-config-' + os.environ['GITHUB_RUN_ID'], '--runtime=runc']
    for value in ('APACHE_RUN_DIR=/tmp', 'APACHE_RUN_USER=nobody', 'APACHE_RUN_GROUP=nogroup', 'APACHE_LOG_DIR=/tmp', 'APACHE_PID_FILE=/tmp/apache.pid'):
        args += ['-e', value]
    args += [images['httpd'], 'sh', '-c', script]
    created = command(*args, timeout=10)
    identifier = created.stdout.strip()
    (work / 'owned-containers.json').write_text(json.dumps([identifier]) + '\n')
    try:
        value = command('sudo', 'docker', 'start', '--attach', identifier, timeout=60, check=False)
    finally:
        remove_owned()
    record.update({'createArgv': args, 'startArgv': ['sudo', 'docker', 'start', '--attach', identifier], 'containerID': identifier, 'exit': value.returncode, 'stdout': value.stdout, 'stderr': value.stderr})
    (results / 'static-configuration.json').write_text(json.dumps(record, indent=2) + '\n')
    assert value.returncode == 0 and not containers(), record
    raise SystemExit(0)

assert mode == 'run'
active = Path((work / 'public-runtime-root').read_text().strip()) / 'active'
records = []
active_process = None
active_observer = None
active_label = None


def interrupted(number, frame):
    if active_process is not None and active_process.poll() is None:
        os.killpg(active_process.pid, signal.SIGTERM)
        try:
            active_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(active_process.pid, signal.SIGKILL)
            active_process.wait(timeout=2)
    if active_label is not None:
        (work / (active_label + '-observer-stop')).write_text('signal\n')
    if active_observer is not None and active_observer.poll() is None:
        try:
            active_observer.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(active_observer.pid, signal.SIGKILL)
            active_observer.wait(timeout=2)
    raise SystemExit(128 + number)


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)


def verify(label):
    for path, manifest, kind in [(active, results / 'base/frozen-fileset.json', 'runtime'), (work / 'harness', results / 'frozen-harness.json', 'harness')]:
        command('python3', str(support / 'fileset.py'), 'verify', str(path), str(manifest), str(results / f'{label}-{kind}.json'), timeout=60)
    for name, identifier in images.items():
        assert command('sudo', 'docker', 'image', 'inspect', '--format', '{{.Id}}', f'gvisor.dev/images/benchmarks/{name}:latest').stdout.strip() == identifier
    config = json.loads(command('sudo', 'cat', '/etc/docker/daemon.json').stdout)['runtimes']['systrap']
    assert config['path'] == str(active / 'runsc')
    assert config['runtimeArgs'] == ['--platform=systrap', '--network=sandbox', '--debug=false']
    command('sudo', 'python3', str(support / 'quiescent.py'), str(active), timeout=10)


def visit(runtime):
    global active_process, active_observer, active_label
    label = 'httpd-' + runtime
    assert not containers()
    verify(label + '-before')
    (work / 'active-visit').write_text(label + '\n')
    output = results / (label + '-output')
    output.mkdir()
    temporary = work / 'bench-tmp'
    temporary.mkdir(exist_ok=True)
    runfiles = work / 'harness/httpd_test.runfiles'
    selector = r'BenchmarkContinuousHttpd\z/filesize[.]1Mb\z/concurrency[.]1000\z'
    args = ['sudo', 'env', f'RUNTIME={runtime}', 'GOMAXPROCS=4', f'RUNFILES_DIR={runfiles}', f'TEST_SRCDIR={runfiles}', 'TEST_WORKSPACE=_main', f'TMPDIR={temporary}', f'TEST_TMPDIR={temporary}', f'TEST_UNDECLARED_OUTPUTS_DIR={output}', str(work / 'harness/httpd_test'), f'-runtime={runtime}', '-test.run=NoTests', f'-test.bench={selector}', '-test.benchtime=1000x', '-test.count=1', '-test.cpu=4', '-test.timeout=150s']
    (results / (label + '-argv.json')).write_text(json.dumps(args, indent=2) + '\n')
    active_label = label
    observer = subprocess.Popen(['python3', str(support / 'observe.py'), str(work), str(results), label, runtime], start_new_session=True)
    active_observer = observer
    ready = work / (label + '-observer-ready')
    deadline = time.monotonic() + 5
    while not ready.exists() and observer.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if not ready.exists():
        if observer.poll() is None:
            os.killpg(observer.pid, signal.SIGKILL)
            observer.wait(timeout=2)
        raise RuntimeError('Observer did not initialize; no workload launched')
    process = None
    start = time.monotonic()
    try:
        with (results / (label + '.log')).open('w') as stream:
            process = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            active_process = process
            try:
                rc = process.wait(timeout=160)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                rc = 124
    finally:
        (work / (label + '-observer-stop')).write_text('stop\n')
        try:
            observer.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(observer.pid, signal.SIGKILL)
            observer.wait()
        left = containers()
        (work / 'owned-containers.json').write_text(json.dumps(left) + '\n')
        remove_owned()
    active_process = active_observer = active_label = None
    verify(label + '-after')
    record = {'runtime': runtime, 'exit': rc, 'seconds': time.monotonic() - start, 'observerExit': observer.returncode, 'leftoverContainers': left}
    records.append(record)
    expected = {'BenchmarkContinuousHttpd/filesize.1Mb/concurrency.1000-4': 1000}
    raw = subprocess.run(['sudo', 'python3', str(support / 'raw_output.py'), str(output / 'raw-clients'), 'httpd'], input=json.dumps(expected), capture_output=True, text=True, timeout=20)
    record['rawParserExit'] = raw.returncode
    record['rawParserStderr'] = raw.stderr
    record['rawProof'] = json.loads(raw.stdout) if raw.returncode == 0 else None
    observed = json.loads((results / (label + '-observer.json')).read_text())
    clients, servers = final_subjects(observed, images, runtime)
    record['finalClientIdentities'] = clients
    record['finalServerIdentities'] = servers
    record['dynamicEvidence'] = dynamic_evidence(observed, servers)
    record['missingDynamicProbeEvidence'] = not record['dynamicEvidence']['usableDynamicEvidence']
    record['missingStopTimeEvidence'] = [identifier for identifier in clients + servers if not observed['containers'][identifier]['stopTimeAvailableAtInspection']]
    record['allRequestAccountingComplete'] = request_accounting_complete(record['rawProof'])
    record['diagnosticCaptureComplete'] = rc == 0 and not left and observer.returncode == 0 and observed['complete'] and len(clients) == len(servers) == 1 and record['dynamicEvidence']['usableLogs'] and record['dynamicEvidence']['usableResources'] and record['allRequestAccountingComplete'] and record['rawProof'] is not None and not record['rawProof']['missingFinalReports'] and not record['rawProof']['missingCalibrationReports']
    record['capabilityValid'] = record['diagnosticCaptureComplete'] and record['rawProof']['allFinalValid'] and record['rawProof']['allRecordsValid']
    (results / 'diagnostic-evidence.json').write_text(json.dumps({'source': os.environ['SOURCE'], 'runtimeBase': '1f33fe3003455db178b97f3b43d3a012f1611994', 'plan': os.environ['PLAN'], 'perturbativeDiagnosticOnly': True, 'visits': records, 'fullCategoryQualified': False}, indent=2) + '\n')


for runtime in ('runc', 'systrap'):
    visit(runtime)
raise SystemExit(0 if all(record['capabilityValid'] for record in records) else 1)
