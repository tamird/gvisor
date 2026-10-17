"""Run the existing StartupEmpty benchmark with two declared runtime releases."""

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import TypedDict
import xml.etree.ElementTree as ET


PACKAGE = Path('test/rbe/state_full_startup')
ARMS = ('before', 'after')
ORDER = (('before', 'after'), ('after', 'before')) * 6
COUNT = 8
RELEASE_FILES = ('runsc', 'containerd-shim-runsc-v1', 'gvisor-bin/checkpointgofer',
                 'gvisor-bin/runsc-metric-server', 'gvisor-bin/runsc-fd-parking',
                 'gvisor-bin/gvisor-sentry-prewarmer', 'gvisor-bin/gvisor_sentry',
                 'gvisor-bin/gvisor-proc-umounter')


class FileIdentity(TypedDict):
    sha256: str
    size: int


def identity(path: Path) -> FileIdentity:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return {'sha256': digest.hexdigest(), 'size': path.stat().st_size}


def check_process(output: Path, release: Path) -> list[int]:
    text = (output / 'test.log').read_text()
    matches = re.findall(r'^BenchmarkStartupEmpty(?:-\d+)?\s+1\s+([0-9]+) ns/op$', text, re.M)
    assert len(matches) == COUNT, (output, len(matches), COUNT)
    values = [int(value) for value in matches]
    assert all(value > 0 for value in values), values
    assert re.search(r'^PASS$', text, re.M), output
    assert not re.search(r'^FAIL(?:\s|$)|^--- (?:FAIL|SKIP):|panic:', text, re.M), output
    report = ET.parse(output / 'test.xml').getroot()
    assert report.tag == 'testsuites', report.tag
    assert not list(report.iter('testcase')), 'Unexpected unit tests selected'
    for tag in ('failure', 'error', 'skipped'):
        assert not list(report.iter(tag)), (output, tag)
    assert all(int(node.get(key, '0')) == 0 for node in report.iter()
               for key in ('failures', 'errors', 'skipped')), output
    assert text.count('Loaded declared image archive ') == 1, output
    declared = re.findall(r'Declared runsc=([^,\n]+), strict sidecars=([^\n]+)', text)
    assert declared == [(str(release / 'runsc'), str(release / 'gvisor-bin'))], declared
    roots = re.findall(r'Private Docker [^\n]+ runtime=runsc, storage=overlay2, [^\n]+ root=(/tmp/gvisor-docker-[^\s]+)', text)
    match roots:
        case [root]:
            # RunTests closes its private daemon before the child exits.
            assert not Path(root).exists() and not Path(root).parent.exists(), root
        case _:
            raise ValueError(f'Expected one private daemon: {roots!r}')
    daemon = (output / 'dockerd.log').read_text()
    assert daemon.count('msg="Starting up"') == daemon.count('msg="Daemon shutdown complete"') == 1, output
    return values


def main() -> int:
    reporter, binary, *arguments = sys.argv[1:]
    assert Path(binary).is_file(), binary
    configs = [arg.split('=', 1)[1] for arg in arguments if arg.startswith('--docker_test_config=')]
    match configs:
        case [config_path]:
            config = json.loads(Path(config_path).read_text())
        case _:
            raise ValueError(f'Expected exactly one Docker input configuration: {configs!r}')
    arguments = [arg for arg in arguments if not arg.startswith('--docker_test_config=')]
    expected = ['-test.v=false', '-test.run=^$', '-test.bench=^BenchmarkStartupEmpty$', '-test.benchtime=1x', '-test.count=8']
    assert arguments[-5:] == expected, arguments
    match config:
        case {'runsc': str(runsc), 'native': False, 'images': [str(archive)],
              'runtime_args': ['--platform=systrap', '--profile'], 'ipv6': False} if len(config) == 5:
            pass
        case _:
            raise ValueError(f'Unexpected declared startup config: {config!r}')
    os.environ.pop('GVISOR_TEST_CLOCK_SOURCE', None)  # Preserve the existing default-clock method.
    assert 'inittrace=' not in os.environ.get('GODEBUG', ''), 'Initialization tracing changes measured startup'
    outer = Path(os.environ['TEST_UNDECLARED_OUTPUTS_DIR']).resolve()
    outer_xml = Path(os.environ['XML_OUTPUT_FILE']).resolve()
    outputs = outer / 'processes'
    outputs.mkdir()
    manifest = json.loads((PACKAGE / 'release-inputs.json').read_text())
    assert set(manifest) == set(ARMS), list(manifest)
    for side in ARMS:
        assert set(manifest[side]) == set(RELEASE_FILES), side
        for name in RELEASE_FILES:
            path = PACKAGE / 'arms' / side / 'release' / name
            assert identity(path) == manifest[side][name] and os.access(path, os.X_OK), (side, name)
    common = {'benchmark': identity(Path(binary)), 'images': {archive: identity(Path(archive))},
              'declaredConfig': config, 'runtimeArgs': [*config['runtime_args'], '--debug=false'],
              'order': ORDER, 'countPerProcess': COUNT, 'pid': os.getpid(), 'uname': list(os.uname()),
              'affinity': sorted(os.sched_getaffinity(0)), 'cpuinfo': Path('/proc/cpuinfo').read_text(),
              'bootID': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'gomaxprocs': os.environ.get('GOMAXPROCS')}
    (outer / 'same-worker-inputs.json').write_text(json.dumps(common, indent=2) + '\n')
    status = 1
    cancelled = 0
    child: subprocess.Popen[bytes] | None = None

    def signal_child(signum: int) -> None:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass  # The child exited between poll and killpg.

    def report_error(error: OSError) -> None:
        try:
            print(f'Cannot retain process evidence: {error}', file=sys.stderr)
        except OSError:
            pass  # Preserve a known native failure or cancellation status.

    def cancel(signum: int, _frame: object) -> None:
        nonlocal cancelled
        cancelled = 128 + signum
        signal_child(signal.SIGTERM)

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    completed: list[dict[str, object]] = []
    aggregate = ET.Element('testsuites')
    try:
        for block, arms in enumerate(ORDER):
            for position, side in enumerate(arms):
                if cancelled:
                    return cancelled
                output = outputs / f'{block:02d}-{position}-{side}'
                output.mkdir()
                release = (PACKAGE / 'arms' / side / 'release').resolve()
                selected = dict(config, runsc=str(release / 'runsc'), runtime_args=common['runtimeArgs'])
                selected_path = output / 'docker-config.json'
                selected_path.write_text(json.dumps(selected, indent=2) + '\n')
                argv = [binary, *arguments, '--docker_test_config=' + str(selected_path)]
                environment = dict(os.environ, TEST_UNDECLARED_OUTPUTS_DIR=str(output), XML_OUTPUT_FILE=str(output / 'test.xml'),
                                   TEST_BINARY=binary, GO_TEST_RUN_FROM_BAZEL='1', GO_TEST_WRAP='1')
                (output / 'argv.json').write_text(json.dumps(argv, indent=2) + '\n')
                started = time.monotonic()
                with (output / 'test.log').open('wb') as log:
                    child = subprocess.Popen(argv, env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    if cancelled:
                        signal_child(signal.SIGTERM)
                    while child.poll() is None:
                        try:
                            child.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            if cancelled:
                                try:
                                    child.wait(timeout=20)
                                except subprocess.TimeoutExpired:
                                    signal_child(signal.SIGKILL)
                                    child.wait()
                    code = child.returncode
                child = None
                status = code if code >= 0 else 128 - code
                record: dict[str, object] = {'side': side, 'block': block, 'position': position,
                    'status': status, 'elapsedSeconds': time.monotonic() - started}
                try:
                    (output / 'status.json').write_text(json.dumps(record, indent=2) + '\n')
                except OSError as error:
                    report_error(error)
                    if cancelled or status:
                        return cancelled or status
                    raise
                completed.append(record)
                if cancelled or status:
                    return cancelled or status
                status = 1  # Required evidence failures keep the cohort unsuccessful.
                rows = check_process(output, release)
                record.update(rowsNS=rows, meanNS=sum(rows) / COUNT)
                (output / 'operation-proof.json').write_text(json.dumps(record, indent=2) + '\n')
                print(f'STARTUP_RAW_BEGIN {block:02d}-{position}-{side}', flush=True)
                print((output / 'test.log').read_text(), end='', flush=True)
                print((output / 'dockerd.log').read_text(), end='', flush=True)
                print('STARTUP_PROCESS ' + json.dumps({'argv': argv, 'config': selected, 'record': record}), flush=True)
                print(f'STARTUP_RAW_END {block:02d}-{position}-{side}', flush=True)
                suite = ET.SubElement(aggregate, 'testsuite', name=f'{block:02d}-{position}-{side}', tests='1', failures='0', errors='0')
                ET.SubElement(suite, 'testcase', name='complete-eight-startup-observations')
        (outer / 'process-results.json').write_text(json.dumps(completed, indent=2) + '\n')
        print('STARTUP_INPUTS ' + json.dumps({'releases': manifest, 'common': common}), flush=True)
        print('STARTUP_RESULTS ' + json.dumps(completed), flush=True)
        for side in ARMS:
            rows = [row for row in completed if row['side'] == side]
            assert len(rows) == 12, (side, len(rows))
            lines = ['BenchmarkStartupEmpty 8 %.3f ns/op' % row['meanNS'] for row in rows]
            (outer / f'{side}.bench').write_text('goos: linux\ngoarch: amd64\npkg: gvisor.dev/gvisor/test/benchmarks/base\n' + '\n'.join(lines) + '\n')
        print('STARTUP_BENCHSTAT_BEGIN', flush=True)
        subprocess.run([reporter, str(outer / 'before.bench'), str(outer / 'after.bench')], check=True, timeout=60)
        print('STARTUP_BENCHSTAT_END', flush=True)
        if not cancelled:
            print('PASS', flush=True)
            status = 0
    finally:
        if child is not None and child.poll() is None:
            signal_child(signal.SIGTERM)
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                signal_child(signal.SIGKILL)
                child.wait()
        if status or cancelled or len(completed) != 24 or any('rowsNS' not in row for row in completed):
            suite = ET.SubElement(aggregate, 'testsuite', name='sandbox-startup', tests='1', errors='1')
            case = ET.SubElement(suite, 'testcase', name='process-status-and-evidence')
            ET.SubElement(case, 'error').text = 'A child or required evidence failed; inspect retained outputs.'
        try:
            ET.ElementTree(aggregate).write(outer_xml, encoding='utf-8', xml_declaration=True)
        except OSError as error:
            report_error(error)
            return cancelled or status or 1
    return cancelled or status


if __name__ == '__main__':
    raise SystemExit(main())
