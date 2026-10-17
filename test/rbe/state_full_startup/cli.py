"""Run the preserved fixed CLI batches against two declared releases."""

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET


PACKAGE = Path('test/rbe/state_full_startup')
ARMS = ('before', 'after')


def main() -> int:
    reporter, = sys.argv[1:]
    output = Path(os.environ['TEST_UNDECLARED_OUTPUTS_DIR']).resolve()
    xml = Path(os.environ['XML_OUTPUT_FILE']).resolve()
    cpu = min(os.sched_getaffinity(0))
    manifest = json.loads((PACKAGE / 'release-inputs.json').read_text())
    assert set(manifest) == set(ARMS), manifest.keys()
    for side in ARMS:
        for name, expected in manifest[side].items():
            path = PACKAGE / 'arms' / side / 'release' / name
            body = path.read_bytes()
            assert {'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body)} == expected, (side, name)
            assert body[:4] == b'\x7fELF' and os.access(path, os.X_OK), (side, name)
        release = {'release/' + name: identity for name, identity in manifest[side].items()}
        (output / f'build-{side}-amd64-release.json').write_text(json.dumps(release) + '\n')
    host = {'uname': list(os.uname()), 'cpuinfo': Path('/proc/cpuinfo').read_text(),
            'allowedCpus': sorted(os.sched_getaffinity(0)), 'selectedCpu': cpu,
            'bootID': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    print('CLI_INPUTS ' + json.dumps({'releases': manifest, 'host': host}), flush=True)
    plan = [(f'warmup-{side}', side, 1, False, 'runsc') for side in ARMS]
    for block in range(12):
        for side in (ARMS if block % 2 == 0 else tuple(reversed(ARMS))):
            plan.append((f'sample-{block:02d}-{side}', side, 64, False, 'runsc'))
    plan.extend((f'init-{side}', side, 1, True, 'runsc') for side in ARMS)
    plan.extend((f'init-sentry-{side}', side, 1, True, 'gvisor-bin/gvisor_sentry') for side in ARMS)
    cancelled = 0
    status = 1
    child: subprocess.Popen[bytes] | None = None
    completed = []
    aggregate = ET.Element('testsuites')

    def signal_child(signum: int) -> None:
        if child is not None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    def cancel(signum: int, _frame: object) -> None:
        nonlocal cancelled
        cancelled = 128 + signum
        signal_child(signal.SIGTERM)

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        for phase, side, count, traced, release_file in plan:
            if cancelled:
                return cancelled
            executable = (PACKAGE / 'arms' / side / 'release' / release_file).resolve()
            environment = dict(os.environ, out=str(output), phase_name=phase,
                               source_side=side, CLI_COUNT=str(count),
                               CLI_INITTRACE='1' if traced else '0',
                               CLI_RELEASE_FILE='release/' + release_file,
                               BENCH_CPU=str(cpu))
            with (output / f'{phase}-wrapper.log').open('wb') as log:
                child = subprocess.Popen([sys.executable, str(PACKAGE / 'cli_batch.py'),
                                          str(executable), '--version'], env=environment,
                                         stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True)
                try:
                    child.wait(timeout=660)
                except subprocess.TimeoutExpired:
                    signal_child(signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        signal_child(signal.SIGKILL)
                        child.wait()
                    status = 124
                else:
                    status = child.returncode if child.returncode >= 0 else 128 - child.returncode
                child = None
            print((output / f'{phase}-wrapper.log').read_text(), end='', flush=True)
            if cancelled or status:
                return cancelled or status
            status = 1
            record = json.loads((output / f'{phase}-processes.json').read_text())
            assert record['count'] == len(record['rows']) == count, phase
            assert record['side'] == side and record['inittrace'] == traced, phase
            assert record['affinity'] == [cpu] and record['gomaxprocs'] == 1, phase
            assert all(row['exit'] == 0 and not row['timeout'] for row in record['rows']), phase
            # Preserve original stdout/stderr, including both split-binary init traces,
            # in the ordinary log retained by the common reporter.
            for row in record['rows']:
                for stream in ('stdout', 'stderr'):
                    body = (output / row[stream]['path']).read_bytes()
                    assert hashlib.sha256(body).hexdigest() == row[stream]['sha256'], phase
                    assert len(body) == row[stream]['size'], phase
                    print(f'CLI_STREAM {phase} {row["iteration"]} {stream} ' + json.dumps(body.decode()), flush=True)
            completed.append(record)
            suite = ET.SubElement(aggregate, 'testsuite', name=phase, tests='1', failures='0', errors='0')
            ET.SubElement(suite, 'testcase', name='complete-source-bound-batch')
        outputs = {(output / row['stdout']['path']).read_bytes()
                   for record in completed for row in record['rows']}
        assert len(outputs) == 1, outputs
        for side in ARMS:
            selected = [record for record in completed
                        if record['side'] == side and record['phase'].startswith('sample-')]
            assert len(selected) == 12, (side, len(selected))
            lines = ['BenchmarkRunscVersion 64 %.3f ns/op' %
                     (sum(row['wallNs'] for row in record['rows']) / 64) for record in selected]
            (output / f'{side}.bench').write_text('goos: linux\ngoarch: amd64\npkg: gvisor.dev/gvisor/runsc\n' + '\n'.join(lines) + '\n')
        print('CLI_RESULTS ' + json.dumps(completed), flush=True)
        print('CLI_BENCHSTAT_BEGIN', flush=True)
        subprocess.run([reporter, str(output / 'before.bench'), str(output / 'after.bench')], check=True, timeout=60)
        print('CLI_BENCHSTAT_END', flush=True)
        if not cancelled:
            print('PASS', flush=True)
            status = 0
    finally:
        if child is not None:
            signal_child(signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                signal_child(signal.SIGKILL)
                child.wait()
        if status or cancelled or len(completed) != len(plan):
            suite = ET.SubElement(aggregate, 'testsuite', name='cli-startup', tests='1', errors='1')
            case = ET.SubElement(suite, 'testcase', name='process-status-and-evidence')
            ET.SubElement(case, 'error').text = 'Native process or required batch evidence failed.'
        try:
            ET.ElementTree(aggregate).write(xml, encoding='utf-8', xml_declaration=True)
        except OSError:
            return cancelled or status or 1
    return cancelled or status


if __name__ == '__main__':
    raise SystemExit(main())
