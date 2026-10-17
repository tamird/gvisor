"""Measure fixed batches of fresh runsc --version children on the hosted worker."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

out = Path(os.environ['out'])
phase = os.environ['phase_name']
side = os.environ['source_side']
count = int(os.environ['CLI_COUNT'])
traced = os.environ['CLI_INITTRACE'] == '1'
assert side in ('before', 'after') and count in (1, 64)
release_file = os.environ.get('CLI_RELEASE_FILE', 'release/runsc')
assert release_file in ('release/runsc', 'release/gvisor-bin/gvisor_sentry'), release_file
if release_file != 'release/runsc':
    assert count == 1 and traced, 'Sentry observation is untimed initialization only'
assert sys.argv[2:] == ['--version'], sys.argv
executable = Path(sys.argv[1]).resolve(strict=True)
body = executable.read_bytes()
release = json.loads((out / ('build-' + side + '-amd64-release.json')).read_text())
assert (hashlib.sha256(body).hexdigest(), len(body)) == (release[release_file]['sha256'], release[release_file]['size'])
assert body[:4] == b'\x7fELF'
cpu = int(os.environ['BENCH_CPU'])
os.sched_setaffinity(0, {cpu})
env = dict(os.environ, GOMAXPROCS='1')
env.pop('GODEBUG', None)
if traced:
    env['GODEBUG'] = 'inittrace=1'
record = {'phase': phase, 'side': side, 'releaseFile': release_file, 'executable': str(executable),
          'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body),
          'argv': [str(executable), '--version'], 'cpu': cpu,
          'affinity': sorted(os.sched_getaffinity(0)), 'gomaxprocs': 1,
          'inittrace': traced, 'count': count, 'rows': []}
record_path = out / (phase + '-processes.json')
record_path.write_text(json.dumps(record, indent=2) + '\n')
for iteration in range(count):
    start = time.perf_counter_ns()
    try:
        result = subprocess.run(record['argv'], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, timeout=10)
    except subprocess.TimeoutExpired as error:
        row = {'iteration': iteration, 'wallNs': time.perf_counter_ns() - start,
               'exit': 124, 'timeout': True}
        stdout, stderr = error.stdout or b'', error.stderr or b''
    else:
        row = {'iteration': iteration, 'wallNs': time.perf_counter_ns() - start,
               'exit': result.returncode, 'timeout': False}
        stdout, stderr = result.stdout, result.stderr
    evidence_error = None
    try:
        for name, output in [('stdout', stdout), ('stderr', stderr)]:
            path = out / (phase + '-%02d-' % iteration + name + '.txt')
            path.write_bytes(output)
            row[name] = {'path': path.name, 'sha256': hashlib.sha256(output).hexdigest(), 'size': len(output)}
        record['rows'].append(row)
        record_path.write_text(json.dumps(record, indent=2) + '\n')
    except OSError as error:
        evidence_error = error
        print('Evidence write failed: %s' % error, file=sys.stderr)
    if row['exit']:
        raise SystemExit(row['exit'] if row['exit'] > 0 else 128 - row['exit'])
    if evidence_error is not None:
        raise evidence_error
    assert row['wallNs'] > 0
    assert re.fullmatch(rb'runsc version [^\r\n]+\nspec: [^\r\n]+\n', stdout), stdout
    if traced:
        assert re.search(rb'^init [^\n]+ [0-9]+ bytes, [0-9]+ allocs$', stderr, re.M), stderr
    else:
        assert not stderr, stderr
print(json.dumps({'phase': phase, 'count': count, 'sha256': record['sha256'], 'inittrace': traced}), flush=True)
