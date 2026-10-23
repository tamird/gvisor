#!/usr/bin/env python3
# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Measure recycled allocation in three source-matched binaries on one CPU.

The caller authenticates and stages each independently built binary. Every
sample starts a fresh Go process; normal Go garbage collection remains enabled.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import resource
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


PACKAGE = 'pkg/sentry/pgalloc'
EXECUTABLE = 'pgalloc_test'
SIDES = ('before', 'reviewed', 'corrected')
BENCHMARKS = {f'BenchmarkAllocateRecycled/{size}/{temperature}'
              for size in ('4K', '64K', '2M') for temperature in ('hot', 'cold')}
ORDERS = (
    ('before', 'reviewed', 'corrected'), ('corrected', 'reviewed', 'before'),
    ('reviewed', 'corrected', 'before'), ('before', 'corrected', 'reviewed'),
    ('corrected', 'before', 'reviewed'), ('reviewed', 'before', 'corrected'),
)


@dataclass(frozen=True)
class Sample:
    name: str
    side: str
    benchtime: str
    measured: bool


def cohort() -> list[Sample]:
    rows = [Sample(f'warmup-{side}', side, '100ms', False) for side in SIDES]
    for block in range(16):
        for side in ORDERS[block % len(ORDERS)]:
            rows.append(Sample(f'sample-{block:02d}-{side}', side, '1s', True))
    return rows


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + '\n')


def native_status(code: int) -> int:
    return code if code >= 0 else 128 - code


def child(cpu: int, output: Path, argv: list[str]) -> int:
    # A fresh wrapper makes RUSAGE_CHILDREN refer to this one Go process.
    os.sched_setaffinity(0, {cpu})
    print('BENCH_GOMAXPROCS=' + os.environ['GOMAXPROCS'], flush=True)
    print('BENCH_GC_DEFAULT=' + str('GOGC' not in os.environ and 'GOMEMLIMIT' not in os.environ and 'GODEBUG' not in os.environ), flush=True)
    print(f'Cpus_allowed_list:\t{cpu}', flush=True)
    started_monotonic_ns = time.monotonic_ns()
    started_unix_ns = time.time_ns()
    with subprocess.Popen(argv) as process:
        write_json(output / 'child.json', {
            'pid': process.pid, 'argv': argv,
            'inheritedAffinity': sorted(os.sched_getaffinity(0)),
            'testSrcDir': os.environ['TEST_SRCDIR'],
            'testWorkspace': os.environ['TEST_WORKSPACE'],
        })
        status = native_status(process.wait())
    ended_monotonic_ns = time.monotonic_ns()
    ended_unix_ns = time.time_ns()
    try:
        # This fresh wrapper has reaped only the one Go process tree. CPU time
        # includes its initialization, calibration and measured iterations.
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        write_json(output / 'usage.json', {
            'startedMonotonicNs': started_monotonic_ns,
            'endedMonotonicNs': ended_monotonic_ns,
            'startedUnixNs': started_unix_ns,
            'endedUnixNs': ended_unix_ns,
            'userSeconds': usage.ru_utime, 'systemSeconds': usage.ru_stime,
            'maxRssKiB': usage.ru_maxrss, 'status': status,
        })
        print(f'BENCH_MAXRSS_KIB={usage.ru_maxrss}', flush=True)
    except OSError:
        if status:
            return status
        raise
    return status


def main(package: Path, reporter: Path) -> int:
    package = package.resolve(strict=True)
    output = Path(os.environ['TEST_UNDECLARED_OUTPUTS_DIR']).resolve(strict=True)
    cpu = min(os.sched_getaffinity(0))
    assert platform.machine() == 'x86_64', platform.machine()
    plan = cohort()
    outer_xml = Path(os.environ['XML_OUTPUT_FILE']).resolve()
    write_json(output / 'selection.json', [asdict(row) for row in plan])
    cpuinfo = Path('/proc/cpuinfo').read_text()
    selected = [dict(line.split(':', 1) for line in block.splitlines() if ':' in line)
                for block in cpuinfo.split('\n\n') if block.strip()]
    selected = [{key.strip(): value.strip() for key, value in block.items()} for block in selected]
    selected = [block for block in selected if block.get('processor') == str(cpu)]
    assert len(selected) == 1, (cpu, selected)
    flags = selected[0].get('flags', '').split()
    write_json(output / 'host.json', {
        'uname': list(os.uname()), 'cpuinfo': cpuinfo,
        'selectedProcessor': selected[0], 'selectedProcessorFeatures': {'erms': 'erms' in flags, 'avx2': 'avx2' in flags},
        'allowedCpus': sorted(os.sched_getaffinity(0)), 'selectedCpu': cpu,
        'bootID': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
    })
    manifest = json.loads((package / 'inputs.json').read_text())
    required = {f'arms/{side}/_main/{PACKAGE}/{EXECUTABLE}' for side in SIDES}
    assert set(manifest['binaries']) == required, (set(manifest['binaries']), required)
    for relative, identity in manifest['binaries'].items():
        path = package / relative
        assert path.stat().st_size == identity['size'], relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == identity['sha256'], relative
        assert os.access(path, os.X_OK), relative
    assert reporter.stat().st_size == manifest['benchstat']['bytes'], str(reporter)
    assert hashlib.sha256(reporter.read_bytes()).hexdigest() == manifest['benchstat']['sha256'], str(reporter)
    assert os.access(reporter, os.X_OK), str(reporter)
    write_json(output / 'inputs.json', manifest)
    for name in ('selection.json', 'host.json', 'inputs.json'):
        print(f'PAIR_INPUT {name} ' + (output / name).read_text(), flush=True)
    cancellation = 0
    process: subprocess.Popen[bytes] | None = None

    def signal_group(signum: int) -> None:
        if process is not None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass  # The process group already exited.

    def cancel(signum: int, _frame: object) -> None:
        nonlocal cancellation
        cancellation = 128 + signum
        signal_group(signal.SIGTERM)

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    benchfiles: dict[str, list[str]] = {side: [] for side in SIDES}
    completed = []
    status = 1
    try:
        for sample in plan:
            if cancellation:
                return cancellation
            directory = output / sample.name
            directory.mkdir()
            arm = package / 'arms' / sample.side
            binary = arm / '_main' / PACKAGE / EXECUTABLE
            relative = str(binary.relative_to(package))
            assert relative in manifest['binaries'], relative
            argv = [str(binary), '-test.run=^$', '-test.bench=^BenchmarkAllocateRecycled$',
                    '-test.benchtime=' + sample.benchtime, '-test.benchmem',
                    '-test.count=1', '-test.cpu=1', '-test.v=false']
            environment = dict(os.environ, GOMAXPROCS='1', GO_TEST_WRAP_TESTV='0',
                TEST_SRCDIR=str(arm), TEST_WORKSPACE='_main', RUNFILES_DIR=str(arm),
                TEST_UNDECLARED_OUTPUTS_DIR=str(directory), XML_OUTPUT_FILE=str(directory / 'test.xml'),
                TEST_TMPDIR=str(directory / 'tmp'), TEST_BINARY=str(binary), GO_TEST_RUN_FROM_BAZEL='1', GO_TEST_WRAP='1')
            for variable in ('GOGC', 'GOMEMLIMIT', 'GODEBUG', 'TESTBRIDGE_TEST_ONLY'):
                environment.pop(variable, None)
            (directory / 'tmp').mkdir()
            started = time.monotonic()
            timed_out = False
            with (directory / 'test.log').open('wb') as log:
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child',
                    str(cpu), str(directory), *argv], env=environment, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    while process.poll() is None and not cancellation:
                        remaining = 60 - (time.monotonic() - started)
                        if remaining <= 0:
                            timed_out = True
                            break
                        try:
                            process.wait(timeout=min(remaining, 1))
                        except subprocess.TimeoutExpired:
                            pass
                finally:
                    if process.poll() is None:
                        signal_group(signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            signal_group(signal.SIGKILL)
                            process.wait()
                status = cancellation or (124 if timed_out else native_status(process.returncode))
                if cancellation or timed_out:
                    # The wrapper can exit before its Go descendant; retain the
                    # process-group identity until descendants are terminated.
                    signal_group(signal.SIGKILL)
                process = None
            record = dict(asdict(sample), status=status, elapsedSeconds=time.monotonic() - started)
            try:
                write_json(directory / 'status.json', record)
            except OSError:
                if status:
                    return status
                raise
            print(f'PAIR_RAW_BEGIN {sample.name}', flush=True)
            print((directory / 'test.log').read_text(), end='', flush=True)
            for name in ('status.json', 'child.json', 'usage.json', 'test.xml'):
                path = directory / name
                if path.exists():
                    print(f'PAIR_FILE {sample.name} {name} ' + path.read_text(), flush=True)
            print(f'PAIR_RAW_END {sample.name}', flush=True)
            if status:
                return status
            status = 1  # Evidence failures must not turn a successful child into a successful cohort.
            text = (directory / 'test.log').read_text().replace('\x16', '')
            assert re.search(r'^PASS$', text, re.M) and not re.search(r'^FAIL(?:\s|$)', text, re.M), sample.name
            assert 'BENCH_GOMAXPROCS=1' in text and 'BENCH_GC_DEFAULT=True' in text, sample.name
            assert re.search(r'^Cpus_allowed_list:\s*' + str(cpu) + r'\s*$', text, re.M), sample.name
            rss = re.findall(r'^BENCH_MAXRSS_KIB=(\d+)$', text, re.M)
            match rss:
                case [value] if int(value) > 0:
                    record['maxRssKiB'] = int(value)
                case _:
                    raise ValueError(f'{sample.name}: invalid RSS report {rss!r}')
            bootstrap = json.loads((directory / 'child.json').read_text())
            assert bootstrap['argv'] == argv and bootstrap['inheritedAffinity'] == [cpu], bootstrap
            usage = json.loads((directory / 'usage.json').read_text())
            assert usage['status'] == record['status'] == 0 and usage['maxRssKiB'] == record['maxRssKiB'], usage
            assert 0 < usage['startedMonotonicNs'] < usage['endedMonotonicNs'], usage
            assert usage['startedUnixNs'] > 0 and usage['endedUnixNs'] > 0, usage
            assert all(math.isfinite(usage[key]) and usage[key] >= 0
                       for key in ('userSeconds', 'systemSeconds')), usage
            record['processUsage'] = usage
            document = ET.parse(directory / 'test.xml')
            assert not any(document.findall('.//' + tag) for tag in ('failure', 'error', 'skipped')), sample.name
            assert all(int(node.get(key, '0')) == 0 for node in document.iter() for key in ('failures', 'errors', 'skipped')), sample.name
            lines = [line for line in text.splitlines() if re.match(r'^Benchmark\S+\s+\d+\s+', line)]
            names = [re.sub(r'-1$', '', line.split()[0]) for line in lines]
            assert len(names) == len(BENCHMARKS) and set(names) == BENCHMARKS, (sample.name, names)
            rows = []
            for name, line in zip(names, lines):
                parts = line.split()
                assert len(parts) >= 4 and len(parts) % 2 == 0 and int(parts[1]) > 0, (sample.name, line)
                units = parts[3::2]
                assert len(units) == len(set(units)), (sample.name, line)
                metrics = {parts[i + 1]: float(parts[i]) for i in range(2, len(parts), 2)}
                assert {'ns/op', 'MB/s', 'B/op', 'allocs/op'} <= metrics.keys(), (sample.name, metrics)
                assert all(math.isfinite(value) and value >= 0 for value in metrics.values()), (sample.name, metrics)
                assert metrics['ns/op'] > 0 and metrics['MB/s'] > 0, (sample.name, metrics)
                rows.append({'benchmark': name, 'metrics': metrics, 'row': line})
            record['benchmarks'] = rows
            completed.append(record)
            if sample.measured:
                benchfiles[sample.side].extend(['pkg: gvisor.dev/gvisor/' + PACKAGE, *lines])
        write_json(output / 'process-results.json', completed)
        for side, lines in benchfiles.items():
            (output / f'{side}.bench').write_text('goos: linux\ngoarch: amd64\n' + '\n'.join(lines) + '\n')
        print('PAIR_RESULTS ' + json.dumps(completed), flush=True)
        print('PAIR_BENCHSTAT_BEGIN', flush=True)
        subprocess.run([str(reporter), *(str(output / f'{side}.bench') for side in SIDES)], check=True, timeout=60)
        print('PAIR_BENCHSTAT_END', flush=True)
        if cancellation:
            return cancellation
        print('PASS', flush=True)
        status = 0
        return cancellation or status
    finally:
        if process is not None:
            signal_group(signal.SIGKILL)
            process.wait()
        write_json(output / 'process-results.json', completed)
        failed = bool(cancellation or status or len(completed) != len(plan))
        document = ET.Element('testsuites')
        suite = ET.SubElement(document, 'testsuite', name='pgalloc-recycled-processes',
            tests=str(len(completed) + int(failed)), failures=str(int(failed)), errors='0')
        for row in completed:
            ET.SubElement(suite, 'testcase', name=row['name'], time=str(row['elapsedSeconds']))
        if failed:
            case = ET.SubElement(suite, 'testcase', name='incomplete-cohort')
            ET.SubElement(case, 'failure').text = 'A native child or required evidence failed; inspect retained outputs.'
        try:
            ET.ElementTree(document).write(outer_xml, encoding='utf-8', xml_declaration=True)
        except OSError:
            return cancellation or status or 1
        if cancellation:
            return cancellation


if __name__ == '__main__':
    match sys.argv[1:]:
        case ['--child', cpu, output, *argv] if argv:
            raise SystemExit(child(int(cpu), Path(output), argv))
        case [reporter]:
            raise SystemExit(main(Path(__file__).absolute().parent, Path(reporter).resolve(strict=True)))
        case _:
            raise SystemExit('Expected BENCHSTAT, or --child CPU OUTPUT EXECUTABLE ARGS')
