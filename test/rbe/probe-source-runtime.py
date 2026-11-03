"""Qualify native source-runtime installation through the existing Buildkite hooks."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import time
import traceback
from datetime import datetime, timezone


TMP = Path(os.environ['RUNNER_TEMP'])
OUT = TMP / 'source-verdict.json'
PRIVATE = TMP / 'source-runtime-private'
PRIVATE.mkdir(mode=0o700, exist_ok=True)
PRODUCT = '88cf79c935336ff38f4a50cc9a7934f942828b28'
INTEGRATION = '2e9e15266979cfb4a75f4a0ef9ef23b2948fda62'
COMPILER = 'a385bd2b0155b82faf4fc6a0c195a153ad57550c'
REAL_BAZEL = Path(os.environ['REAL_BAZEL'])
SECRET = os.environ.pop('BUILDBUDDY_API_KEY')
CACHE = '--remote_cache=grpcs://remote.buildbuddy.io'
METADATA = '--build_metadata=RUNTIME_INSTALL=Buildkite source job'
EXPECTED = {
    'runsc', 'containerd-shim-runsc-v1',
    'gvisor-bin/checkpointgofer', 'gvisor-bin/gvisor-proc-umounter',
    'gvisor-bin/gvisor-sentry-prewarmer', 'gvisor-bin/gvisor_sentry',
    'gvisor-bin/runsc-fd-parking', 'gvisor-bin/runsc-metric-server',
}
record = {
    'product': PRODUCT, 'integration': INTEGRATION, 'compiler': COMPILER,
    'status': 'INCOMPLETE', 'phases': {}, 'observations': {},
    'scope': 'native ARM64 source installation; retained compiler prerequisite',
    'producer_action_freshness': 'not asserted',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save():
    data = json.dumps(record, indent=2, sort_keys=True) + '\n'
    assert len(data.encode()) < 128 * 1024
    assert SECRET and SECRET not in data and json.dumps(SECRET)[1:-1] not in data
    assert not re.search(r'(?i)(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_|(?:authorization|x-buildbuddy-api-key)\s*[:=])', data)
    OUT.write_text(data)


def command(name, args, *, env=None, timeout=180, expected=0):
    path = PRIVATE / (name + '.log')
    row = {'started_at': now()}
    record['phases'][name] = row
    save()
    started = time.monotonic()
    with path.open('wb') as stream:
        process = subprocess.Popen(args, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            code = 124
    row.update(exit=code, finished_at=now(), elapsed_seconds=time.monotonic() - started,
               log_sha256=digest(path), log_bytes=path.stat().st_size)
    # Only fixed diagnostic classes leave the private log.
    text = path.read_text(errors='replace')
    row['diagnostic_classes'] = [label for label, pattern in (
        ('unknown_option', r'Unrecognized option|unrecognized option|Unknown startup option'),
        ('missing_sentry', r'gvisor_sentry.*(?:no such file|not found)'),
        ('repository_fetch', r'Error downloading|error loading package|fetch of repository'),
        ('timeout', r'Timeout|timed out'),
    ) if re.search(pattern, text)]
    row['diagnostics'] = []
    for line in text.splitlines():
        if not re.match(r'^(?:ERROR:|FATAL:|Error:|[A-Za-z]+Error:|make(?:\[\d+\])?: \*\*\*)', line):
            continue
        original_sha = hashlib.sha256(line.encode()).hexdigest()
        line = re.sub(r'"(?:\\.|[^"\\])*"', '<quoted>', line)
        line = re.sub(r'(?i)(?:https?|grpcs?)://\S+', '<url>', line)
        line = re.sub(r'(?:[A-Za-z]:)?/[^\s,;]*', '<path>', line)
        if SECRET in line or re.search(r'(?i)(?:authorization|x-buildbuddy-api-key|github_pat_|gh[pousr]_\w|Bearer\s+)', line):
            line = '<diagnostic sha256=' + original_sha + '>'
        row['diagnostics'].append({'message': line[:600], 'original_sha256': original_sha})
        if len(row['diagnostics']) == 8:
            break
    row['expected_exit'] = expected
    row['status'] = 'PASS' if code == expected else ('TIMEOUT' if code == 124 else 'FAIL')
    save()
    assert code == expected, name
    return path


def captured(args, *, env=None):
    return subprocess.check_output(args, text=True, env=env).strip()


def environment(name, **updates):
    env = dict(os.environ, PATH=str(PRIVATE / 'tools') + ':' + os.environ['PATH'],
               DOCKER_BUILD='true', BUILDKITE_PIPELINE_INSTALL_RUNTIME='true',
               RUNTIME='native-' + name, RUNTIME_ARGS='', STAGED_BINARIES='',
               DOCKER_RELOAD_COMMAND='sudo systemctl reload docker',
               BAZEL_REMOTE_CACHE=CACHE,
               BAZEL_OPTIONS='--build_metadata="RUNTIME_INSTALL=Buildkite source job"',
               CLIENT_PHASE=name, ENV_OUT=str(PRIVATE / (name + '.env.json')))
    env.pop('BUILDKITE_BAZEL_DIRECTORY', None)
    env.pop('BUILDKITE_STAGED_BINARIES_DIRECTORY', None)
    env.update(updates)
    return env


def hook(name, env, *, expected=0, timeout=3300):
    # The EXIT recorder does not alter the hook's native status.
    script = 'set -euo pipefail\ntrap \'rc=$?; python3 "$EMIT_ENV"; exit "$rc"\' EXIT\n' + pre
    path = command(name, ['bash', '-c', script], env=env, timeout=timeout, expected=expected)
    values = json.loads(Path(env['ENV_OUT']).read_text())
    env.update({key: value for key, value in values.items() if value is not None})
    if values['BUILDKITE_BAZEL_DIRECTORY']:
        env['PATH'] = values['BUILDKITE_BAZEL_DIRECTORY'] + ':' + env['PATH']
    return path


def clients(name):
    path = PRIVATE / (name + '.argv.jsonl')
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def runtime(env):
    config = json.loads(captured(['sudo', 'cat', '/etc/docker/daemon.json']))
    value = config['runtimes'][env['RUNTIME']]
    staging = Path(env['BUILDKITE_STAGED_BINARIES_DIRECTORY'])
    assert value['path'] == str(staging / 'runsc')
    assert value.get('runtimeArgs', []) == env['RUNTIME_ARGS'].split()
    return staging, value


def inventory(directory):
    result = {}
    assert {str(p.relative_to(directory)) for p in directory.rglob('*') if p.is_file()} == EXPECTED
    for name in sorted(EXPECTED):
        path = directory / name
        assert path.is_file() and not path.is_symlink()
        with path.open('rb') as stream:
            header = stream.read(20)
        assert header[:6] == b'\x7fELF\x02\x01' and int.from_bytes(header[18:20], 'little') == 183
        assert os.access(path, os.X_OK)
        result[name] = {'sha256': digest(path), 'bytes': path.stat().st_size,
                        'mode': oct(path.stat().st_mode & 0o777), 'elf_machine': 183}
    return result


def docker_guest(name, env):
    identifier = captured(['docker', 'create', '--network=none', '--runtime', env['RUNTIME'], 'hook-probe', '/bin/true'])
    try:
        command(name, ['docker', 'start', '-a', identifier], timeout=90)
        value = json.loads(captured(['docker', 'inspect', identifier]))[0]
        assert value['HostConfig']['Runtime'] == env['RUNTIME']
        assert value['State']['Status'] == 'exited' and value['State']['ExitCode'] == 0
        record['observations'][name] = {'runtime': env['RUNTIME'], 'state': 'exited', 'exit': 0}
        save()
    finally:
        subprocess.run(['docker', 'rm', '-f', identifier], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def cleanup(name, env):
    env = dict(env, BUILDKITE_COMMAND_EXIT_STATUS='1')
    command(name, ['bash', '-euc', post], env=env)
    if env.get('BUILDKITE_BAZEL_DIRECTORY'):
        assert not Path(env['BUILDKITE_BAZEL_DIRECTORY']).exists()
    if env.get('BUILDKITE_STAGED_BINARIES_DIRECTORY'):
        assert not Path(env['BUILDKITE_STAGED_BINARIES_DIRECTORY']).exists()


try:
    assert platform.system() == 'Linux' and platform.machine() == 'aarch64' and os.getuid() != 0
    assert captured(['git', 'rev-parse', 'HEAD']) == INTEGRATION
    record['execution_platform'] = {'architecture': 'arm64', 'isolation': 'oci',
        'image': 'docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02',
        'routing_source': '0b014096d2f890fc8441d64aa585cb002ec77728'}
    record['host'] = {'os': 'Linux', 'architecture': platform.machine(), 'kernel': platform.release(), 'uid': os.getuid()}
    original = Path('.buildkite/hooks/pre-command').read_text()
    begin = original.index('# The job and its post-command cleanup must use the same Bazel server.')
    end = original.index('# Helper for benchmarks, based on the branch.')
    pre = original[begin:end]
    post_text = Path('.buildkite/hooks/post-command').read_text()
    post = post_text[post_text.index('if [[ "${BUILDKITE_PIPELINE_INSTALL_RUNTIME:-}" == "true" ]]; then'):]
    assert 'make copy DOCKER_BUILD=false TARGETS=//:release' in pre
    assert 'go install github.com/bazelbuild/bazelisk@v1.30.0' in pre
    assert post.count('bazel shutdown || shutdown_status=$?') == 1
    rc = PRIVATE / 'client.bazelrc'
    rc.write_text('\n'.join([
        'build --remote_executor=grpcs://remote.buildbuddy.io',
        'build --remote_header=x-buildbuddy-api-key=' + SECRET,
        'build --spawn_strategy=remote', 'build --noremote_local_fallback', 'build --jobs=32',
        'build --strategy=GoStdlib=processwrapper-sandbox',
        'build --strategy=GoStdlibList=processwrapper-sandbox',
        'build --modify_execution_info=GoStdlib(List)?=+no-remote-cache',
        'build --inject_repository=hook_platform=' + str(TMP / 'gnu'),
        'build --host_platform=@hook_platform//:gnu',
        'build --extra_execution_platforms=@hook_platform//:gnu',
    ]) + '\n')
    os.chmod(rc, 0o600)
    tools = PRIVATE / 'tools'
    tools.mkdir()
    emitter = PRIVATE / 'emit.py'
    emitter.write_text('import json,os\nfrom pathlib import Path\nPath(os.environ["ENV_OUT"]).write_text(json.dumps({k:os.environ.get(k) for k in ("BUILDKITE_BAZEL_DIRECTORY","BUILDKITE_STAGED_BINARIES_DIRECTORY")}))\n')
    os.environ['EMIT_ENV'] = str(emitter)
    # Thin client observation only; original stdout, stderr and exit are forwarded.
    wrapper = tools / 'bazelisk'
    wrapper.write_text('''#!/usr/bin/python3
import json,os,subprocess,sys
from pathlib import Path
p=Path(os.environ['RUNTIME_PRIVATE']); phase=os.environ['CLIENT_PHASE']; args=sys.argv[1:]
f=p/(phase+'.argv.jsonl'); rows=f.read_text().splitlines() if f.exists() else []
with f.open('a') as s:s.write(json.dumps(args)+'\\n')
extra=[]
if args and args[0] in ('build','cquery'):extra=['--build_event_json_file='+str(p/(phase+'-'+str(len(rows))+'.bep'))]
position=args.index('--') if '--' in args else len(args)
r=subprocess.run([os.environ['REAL_BAZEL'],'--bazelrc='+str(p/'client.bazelrc'),*args[:position],*extra,*args[position:]],stdout=subprocess.PIPE)
if args and args[0]=='cquery':(p/(phase+'.cquery')).write_bytes(r.stdout)
sys.stdout.buffer.write(r.stdout);sys.stdout.buffer.flush()
sys.exit(r.returncode if r.returncode>=0 else 128-r.returncode)
''')
    wrapper.chmod(0o755)
    (tools / 'docker').write_text('#!/bin/sh\nprintf called >> "$RUNTIME_PRIVATE/docker-called"\nexit 87\n')
    (tools / 'docker').chmod(0o755)
    os.environ['RUNTIME_PRIVATE'] = str(PRIVATE)
    record['bazel_version'] = captured([str(REAL_BAZEL), '--version'])
    assert record['bazel_version'] == 'bazel 8.8.1'
    record['bazelisk_sha256'] = digest(REAL_BAZEL)
    source = environment('source-empty')
    hook('source-empty', source)
    assert not (PRIVATE / 'docker-called').exists()
    source_dir, installed = runtime(source)
    rows = clients('source-empty')
    assert len(rows) == 2 and [row[0] for row in rows] == ['build', 'cquery']
    for row in rows:
        assert row.count(CACHE) == 1 and row.count(METADATA) == 1
    assert rows[0][-2:] == ['--', '//:release']
    assert rows[1][-2:] == ['--', 'set() union "//:release"']
    files = inventory(source_dir)
    outputs = {}
    for line in (PRIVATE / 'source-empty.cquery').read_text().splitlines():
        path, destination = line.split()
        assert path.startswith('bazel-out/') and destination in EXPECTED
        assert destination not in outputs
        producer = Path(path).resolve()
        assert producer.is_file() and digest(producer) == files[destination]['sha256']
        assert producer.stat().st_size == files[destination]['bytes']
        outputs[destination] = producer
    assert set(outputs) == EXPECTED
    for number in range(2):
        events = [json.loads(line) for line in (PRIVATE / f'source-empty-{number}.bep').read_text().splitlines()]
        options = [event['optionsParsed']['explicitCmdLine'] for event in events if 'optionsParsed' in event]
        assert len(options) == 1 and options[0].count(CACHE) == 1 and options[0].count(METADATA) == 1
    record['observations']['source_install'] = {'files': files, 'runtime_args': installed.get('runtimeArgs', []),
        'docker_invocations_during_install': 0, 'native_make_build_and_cquery': True,
        'cache_argument_once_each': True, 'quoted_metadata_once_each': True,
        'executable_architecture': 'aarch64', 'producer_files_match': True,
        'build_target': '//:release', 'cquery_expression': 'set() union "//:release"'}
    command('clean_outputs', [str(Path(source['BUILDKITE_BAZEL_DIRECTORY']) / 'bazel'), 'clean'], env=source)
    assert all(not path.exists() for path in outputs.values())
    assert inventory(source_dir) == files
    record['observations']['durable_after_clean'] = True
    docker_guest('source_guest_after_clean', source)
    pid = int(captured([str(Path(source['BUILDKITE_BAZEL_DIRECTORY']) / 'bazel'), 'info', 'server_pid'], env=source))
    assert Path(f'/proc/{pid}').exists()
    cleanup('failed_mixed_job_cleanup', source)
    for _ in range(100):
        if not Path(f'/proc/{pid}').exists():
            break
        time.sleep(0.1)
    assert not Path(f'/proc/{pid}').exists()
    record['observations']['failed_mixed_job_cleanup'] = {'job_exit_status': 1, 'native_server_stopped': True,
        'client_directory_removed': True, 'runtime_directory_removed': True}
    record['status'] = 'PASS'
except Exception as exc:
    record['error_type'] = type(exc).__name__
    frame = traceback.extract_tb(exc.__traceback__)[-1]
    record['error_location'] = {'file': 'probe-source-runtime.py', 'line': frame.lineno}
    if any(row.get('status') == 'FAIL' for row in record['phases'].values()):
        record['status'] = 'FAIL'
finally:
    # Bind original client invocations without publishing options, logs or environment.
    invocations = []
    for path in sorted(PRIVATE.glob('*.bep')):
        row = {'receipt': path.name, 'complete_json': True}
        with path.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    row['complete_json'] = False
                    break
                if 'started' in event:
                    value = event['started'].get('uuid', '')
                    if re.fullmatch(r'[0-9a-f-]{36}', value):
                        row['invocation_id'] = value
                if 'buildMetrics' in event:
                    summary = event['buildMetrics'].get('actionSummary', {})
                    row['runner_counts'] = {entry['name']: int(entry.get('count', 0))
                                            for entry in summary.get('runnerCount', [])
                                            if re.fullmatch('[A-Za-z0-9 _/.-]{1,64}', entry['name'])}
                if 'finished' in event:
                    row['exit'] = event['finished'].get('exitCode', {}).get('code')
        invocations.append(row)
    record['invocations'] = invocations
    record['verdict_at'] = now()
    save()
    # The original bootstrap is job-scoped; leave no native server or private key.
    with (PRIVATE / 'final-cleanup.log').open('wb') as stream:
        subprocess.run([str(REAL_BAZEL), 'shutdown'], stdout=stream, stderr=subprocess.STDOUT, timeout=60, check=False)
    rc_path = PRIVATE / 'client.bazelrc'
    rc_path.unlink(missing_ok=True)
    for name in ('bazel', 'bazelisk'):
        (REAL_BAZEL.parent / name).unlink(missing_ok=True)
    REAL_BAZEL.parent.rmdir()

print(json.dumps({'status': record['status'], 'verdict_at': record['verdict_at']}))
raise SystemExit(0 if record['status'] == 'PASS' else 1)
