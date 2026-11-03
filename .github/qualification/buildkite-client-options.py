#!/usr/bin/env python3
"""Compare native Buildkite entry points without executing package builds."""
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
from threading import Thread
import time
from zipfile import ZipFile

import yaml

SOURCE = '9ae6638ae6d1916725b10f45a5cad76f022d9d7e'
BASE = '75c04f42d5b7346e4a54a5991dfaf31b043b6769'
PKG = 'bcec52ddaaddedf04de03988486d72647187e309'
STAGED = '8a6244298d8858f8984ab18b58532a0a64d3053b'
OLD_PKG = 'd30816db365b44801393af8fe721795897ab5a65'
OLD_STAGED = 'a5627c7311e6acaed4179f3ebd6a0694661336d9'
TMP = Path(os.environ['RUNNER_TEMP']) / 'client-options'
META = '--build_metadata=CLIENT_CHECK=Buildkite quoted options'
TARGET = '//tools/stagedcheck'


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def call(argv, env=None):
    p = subprocess.run(argv, env=env, capture_output=True, text=True)
    if p.returncode:
        errors = [line[:450] for line in p.stderr.splitlines() if line.startswith('ERROR:')]
        raise ValueError(f'{argv[0]} exit {p.returncode}: {errors[:3]}')
    return p.stdout


def wrapped():
    arm = os.environ['CHECK_ARM']
    name = Path(sys.argv[0]).name
    if name == 'docker':
        write(TMP / f'{arm}-docker.json', {'called': True})
        return 91
    if name == 'gcloud':
        assert sys.argv[1:3] == ['storage', 'cat'] and len(sys.argv) == 4
        fixture = Path(os.environ['CHECK_FIXTURE'])
        sys.stdout.buffer.write(fixture.read_bytes())
        return 23 if sys.argv[3] == 'download-failure' else 0
    cmd, *args = sys.argv[1:]
    assert cmd in ('build', 'cquery', 'run'), cmd
    event = TMP / f'{arm}-{cmd}.bep'
    flags = ['--jobs=4', '--build_event_json_file=' + str(event)]
    if cmd == 'build':
        flags.append('--nobuild')
    start = time.monotonic()
    with (TMP / f'{arm}-{cmd}.out').open('wb') as out, (TMP / f'{arm}-{cmd}.err').open('wb') as err:
        p = subprocess.run([os.environ['REAL_BAZEL'], '--host_jvm_args=-Xmx4g', cmd, *flags, *args], stdout=out, stderr=err)
    write(TMP / f'{arm}-{cmd}.json', {'args': args, 'exit': p.returncode, 'seconds': time.monotonic() - start})
    if cmd == 'cquery':
        sys.stdout.buffer.write((TMP / f'{arm}-{cmd}.out').read_bytes())
    return p.returncode


def events(arm, cmd):
    return [json.loads(line) for line in (TMP / f'{arm}-{cmd}.bep').read_text().splitlines()]


def command_record(arm, cmd, env, options):
    row = json.loads((TMP / f'{arm}-{cmd}.json').read_text())
    parsed, = [e['optionsParsed']['explicitCmdLine'] for e in events(arm, cmd) if 'optionsParsed' in e]
    assert (env['BAZEL_REMOTE_CACHE'] in parsed) == options, (arm, cmd, parsed)
    assert (META in parsed) == options, (arm, cmd, parsed)
    row['agent_cache_received'] = options
    row['quoted_option_received'] = options
    return row


def configured(arm):
    rows = []
    for e in events(arm, 'build'):
        if 'targetConfigured' not in e['id'] or 'configured' not in e:
            continue
        owner = e['id']['targetConfigured']
        for child in e.get('children', []):
            if 'targetCompleted' in child:
                target = child['targetCompleted']
                rows.append([owner['label'], owner.get('aspect', ''), e['configured'].get('targetKind', ''), sorted(e['configured'].get('tag', [])), target['configuration']['id']])
    assert rows and all(row[-1] for row in rows), (arm, rows[:3])
    assert not any('actionCompleted' in e['id'] for e in events(arm, 'build')), arm
    return sorted(rows)


def partitions(rows):
    groups = {}
    for label, aspect, kind, tags, config in rows:
        groups.setdefault(config, []).append((label, aspect, kind, tuple(tags)))
    return {key: sorted(value) for key, value in groups.items()}


def pipeline(rev, file, label):
    doc = yaml.safe_load(git('show', f'{rev}:{file}'))
    step, = [s for s in doc['steps'] if isinstance(s, dict) and s.get('label') == label]
    assert step['env']['DOCKER_BUILD'] == 'false', step
    return step


def execute(arm, cmd, env):
    env = env | {'CHECK_ARM': arm}
    p = subprocess.run(cmd, env=env, capture_output=True, text=True)
    row = {'command': cmd, 'exit': p.returncode, 'finished_at': now()}
    write(TMP / f'{arm}-entry.json', row)
    assert not (TMP / f'{arm}-docker.json').exists(), arm
    return row


def producer(arm, env, target, owners):
    args = json.loads((TMP / f'{arm}-build.json').read_text())['args']
    assert args[-1] in ('//pkg/...', '//runsc/...'), args
    args = args[:-1]
    if args[-1] == '--':
        args.pop()
    graph = json.loads(call([env['REAL_BAZEL'], '--host_jvm_args=-Xmx4g', 'aquery', *args, '--output=jsonproto', '--noinclude_artifacts', '--noinclude_commandline', target], env))
    labels = {t['id']: t['label'].removeprefix('@@') for t in graph['targets']}
    configurations = {c['id']: c['checksum'] for c in graph['configuration']}
    # A label can have several configured instances on this Bazel server.
    # Use the configuration selected by this build's target-completed event.
    selected, = {cfg for label, aspect, _, _, cfg in owners if label == target and not aspect}
    actions = sorted((labels[a['targetId']], configurations[a['configurationId']], a['mnemonic'], a['actionKey'])
                     for a in graph['actions'] if a['mnemonic'] in ('GoLink', 'GoCompilePkg'))
    assert actions and all(label == target and len(key) == 64 for label, _, _, key in actions)
    matched = [action for action in actions if action[1] == selected]
    expected = ['GoCompilePkg', 'GoLink'] if target == '//runsc:runsc' else ['GoCompilePkg']
    assert [action[2] for action in matched] == expected, (target, selected, actions)
    return matched, [action for action in actions if action[1] != selected]


def compile_pair(key, old, new, env, row):
    failures = []
    for arm, cmd, options in ((key + '-old', shlex.split(old), False), (key + '-make', shlex.split(new), True)):
        try:
            current = row[arm] = execute(arm, cmd, env)
            current['build'] = command_record(arm, 'build', env, options)
            assert current['exit'] == current['build']['exit'] == 0, current
            current['configured'] = configured(arm)
            if options:
                current['cquery'] = command_record(arm, 'cquery', env, True)
                assert current['cquery']['exit'] == 0
                raw = (TMP / f'{arm}-cquery.out').read_bytes()
                pairs = [line.split() for line in raw.decode().splitlines() if line.strip()]
                assert pairs and all(len(pair) == 2 and not pair[0].startswith('/') for pair in pairs)
                current['cquery'].update(enumerated_outputs=len(pairs), generated_outputs=sum(pair[0].startswith('bazel-out/') for pair in pairs), output_sha256=sha256(raw).hexdigest(), materialized_outputs_claimed=False)
            target = '//pkg/abi/linux:linux' if key.endswith('pkg') else '//runsc:runsc'
            current['selected_producer_actions'], current['other_configurations'] = producer(
                arm, env, target, current['configured'])
        except Exception as exc:
            failures.append(arm)
            row.setdefault(arm, {})['error'] = f'{type(exc).__name__}: {exc}'[:1000]
    assert not failures, failures
    left, right = row[key + '-old'], row[key + '-make']
    a, b = partitions(left['configured']), partitions(right['configured'])
    assert sorted(a.values()) == sorted(b.values()), key
    row['configuration_correspondence'] = {old_id: [new_id for new_id, members in b.items() if members == owners] for old_id, owners in a.items()}
    assert all(len(matches) == 1 for matches in row['configuration_correspondence'].values()), row
    assert left['configured'] == right['configured'], key
    assert left['selected_producer_actions'] == right['selected_producer_actions'], key


def archive(path, names):
    with tarfile.open(path, 'w:gz') as stream:
        for name in names:
            member = tarfile.TarInfo('./' + name)
            member.size = 1
            stream.addfile(member, io.BytesIO(b'x'))


def previous(row):
    hashes = {'checker': '67cabc48d71f7fe0b6a10df90bc567d114ae00c9666074df4a6c63bd489e27f7', 'archives': '97d7b4a4757b07c339488dd5b92078563371752ef93117af590615f9b0d5b8ae'}
    for name, digest in hashes.items():
        path = Path(os.environ['RUNNER_TEMP']) / (name + '.zip')
        assert sha256(path.read_bytes()).hexdigest() == digest
        with ZipFile(path) as z:
            checker = json.loads(z.read('checker.json'))
            assert len(checker['names']) == 8
            row[name] = {'sha256': digest, 'checker': checker}
            if name == 'archives':
                layouts = {name: json.loads(z.read(name)) for name in z.namelist() if name.startswith('gvisor.')}
                assert len(layouts) == 4 and all(layout == next(iter(layouts.values())) for layout in layouts.values())
                assert sorted(n for n, f in next(iter(layouts.values())).items() if f['type'] != '5') == checker['names']
                row['archive_layouts'] = layouts
    for path in ('tools/stagedcheck/BUILD', 'tools/stagedcheck/main.go', 'tools/release.bzl', 'debian/BUILD'):
        assert git('rev-parse', f'HEAD:{path}') == git('rev-parse', f'{OLD_STAGED}:{path}'), path


def staged(env, row):
    # Reuse the original checker fixture and exact byte-qualified member list.
    with ZipFile(Path(os.environ['RUNNER_TEMP']) / 'checker.zip') as z:
        names = json.loads(z.read('checker.json'))['names']
    valid = TMP / 'complete.tar.gz'
    archive(valid, names + ['extra/payload'])
    missing = TMP / 'missing.tar.gz'
    archive(missing, names[1:])
    corrupt = TMP / 'corrupt.tar.gz'
    data = bytearray(valid.read_bytes()); data[-8] ^= 1; corrupt.write_bytes(data)
    truncated = TMP / 'truncated.tar.gz'; truncated.write_bytes(valid.read_bytes()[:-5])
    label = ':package: Staged binaries consistency check'
    old = pipeline(OLD_STAGED, '.buildkite/pipeline.yaml', label)['command'].replace('$$STAGED_BINARIES', '$STAGED_BINARIES')
    new, = pipeline(SOURCE, '.buildkite/pipeline.yaml', label)['commands']
    cases = [('old', old, valid, 0, False, ''), ('complete', new, valid, 0, True, ''), ('missing', new, missing, 2, True, names[0]), ('corrupt', new, corrupt, 2, True, 'checksum'), ('truncated', new, truncated, 2, True, 'unexpected EOF'), ('download', new, valid, 2, True, '')]
    failures = []
    for name, command, fixture, exit_code, options, diagnostic in cases:
        try:
            arm = 'staged-' + name
            current_env = env | {'CHECK_FIXTURE': str(fixture), 'STAGED_BINARIES': 'download-failure' if name == 'download' else str(fixture)}
            item = row[name] = execute(arm, ['bash', '-c', command], current_env)
            item['fixture_sha256'] = sha256(fixture.read_bytes()).hexdigest()
            item['bazel'] = command_record(arm, 'run', env, options)
            stderr = (TMP / f'{arm}-run.err').read_text()
            item['stderr_sha256'] = sha256(stderr.encode()).hexdigest()
            item['diagnostic'] = diagnostic if diagnostic in stderr else None
            assert item['exit'] == exit_code, item
            assert item['bazel']['exit'] == (1 if name in ('missing', 'corrupt', 'truncated') else 0), item
            assert diagnostic in stderr, item
        except Exception as exc:
            failures.append(name)
            row.setdefault(name, {})['error'] = f'{type(exc).__name__}: {exc}'[:1000]
    manifest = Path('bazel-bin/tools/stagedcheck/members.txt').read_text().splitlines()
    assert manifest == names, manifest
    row['names'] = names
    graph = json.loads(call([env['REAL_BAZEL'], '--host_jvm_args=-Xmx4g', 'aquery', '--output=jsonproto', '--noinclude_artifacts', 'deps(//tools/stagedcheck)'], env))
    assert not any(t['label'].removeprefix('@@').startswith(('//runsc:', '//shim:', '//pkg/sentry/')) for t in graph['targets'])
    assert not {'PackageTar', 'PackageDeb', 'ReleaseFiles'} & {a['mnemonic'] for a in graph['actions']}
    row['empty'] = execute('staged-empty', ['make', 'staged-binaries-check'], env | {'STAGED_BINARIES': ''})
    assert row['empty']['exit'] == 0 and not (TMP / 'staged-empty-run.json').exists()
    assert not failures, failures


class Cache(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_error(404)

    def log_message(self, *args):
        pass


def phase(name, fn, result):
    row = result['phases'][name] = {'status': 'FAIL', 'started_at': now()}
    write(TMP / 'result.json', result)
    try:
        fn(row)
        row['status'] = 'PASS'
    except Exception as exc:
        row['error'] = f'{type(exc).__name__}: {exc}'[:2000]
    finally:
        row['finished_at'] = now()
        write(TMP / 'result.json', result)


def main(result):
    assert (platform.system(), platform.machine()) == ('Linux', 'x86_64')
    assert git('rev-parse', 'HEAD') == SOURCE
    assert git('rev-parse', 'HEAD^') == git('rev-parse', STAGED)
    changes = [git('diff', '--binary', a, b) for a, b in (('HEAD^', 'HEAD'), (BASE, PKG))]
    patch_ids = [subprocess.check_output(['git', 'patch-id', '--stable'], input=patch.encode()).decode().split()[0] for patch in changes]
    assert patch_ids[0] == patch_ids[1], patch_ids
    assert git('status', '--porcelain', '--untracked-files=no') == ''
    real = shutil.which('bazel'); assert real
    assert call([real, '--version']).strip() == 'bazel ' + Path('.bazelversion').read_text().strip()
    bindir = TMP / 'bin'; bindir.mkdir()
    for name in ('bazel', 'docker', 'gcloud'):
        (bindir / name).symlink_to(Path(__file__).resolve())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Cache)
    Thread(target=server.serve_forever, daemon=True).start()
    env = os.environ | {'REAL_BAZEL': real, 'PATH': str(bindir) + ':' + os.environ['PATH'], 'DOCKER_BUILD': 'false', 'DOCKER_HOST': 'unix:///absent-buildkite-docker', 'BAZEL_REMOTE_CACHE': f'--remote_cache=http://127.0.0.1:{server.server_port}', 'BAZEL_OPTIONS': shlex.quote(META) + ' --noremote_upload_local_results'}
    result['host'] = {'architecture': platform.machine(), 'kernel': platform.release(), 'cpu_count': os.cpu_count(), 'bazel': call([real, '--version']).strip(), 'gcc': call(['gcc', '--version']).splitlines()[0], 'arm_gcc': call(['aarch64-linux-gnu-gcc', '--version']).splitlines()[0]}
    for architecture in ('AMD64', 'ARM64'):
        label = f':world_map: Build runsc and pkg ({architecture})'
        old = pipeline(OLD_PKG, '.buildkite/pipeline.yaml', label)
        new = pipeline(SOURCE, '.buildkite/pipeline.yaml', label)
        assert old['agents'] == new['agents']
        if architecture == 'AMD64':
            for ref in (OLD_PKG, SOURCE):
                assert pipeline(ref, '.buildkite/first-pass.yaml', label)['commands'] == pipeline(ref, '.buildkite/pipeline.yaml', label)['commands']
        for kind, old_cmd, new_cmd in zip(('pkg', 'runsc'), old['commands'], new['commands'], strict=True):
            key = architecture.lower() + '-' + kind
            phase(key, lambda row, key=key, old_cmd=old_cmd, new_cmd=new_cmd: compile_pair(key, old_cmd, new_cmd, env, row), result)
    def rejected_options(row):
        bad_env = env | {'BAZEL_OPTIONS': env['BAZEL_OPTIONS'] + ' --qualification_missing_option'}
        command = pipeline(SOURCE, '.buildkite/pipeline.yaml', ':world_map: Build runsc and pkg (AMD64)')['commands'][0]
        row.update(execute('invalid-option', shlex.split(command), bad_env))
        detail = json.loads((TMP / 'invalid-option-build.json').read_text())
        row['build'] = detail
        assert row['exit'] == 2 and detail['exit'] == 2, row
        assert not (TMP / 'invalid-option-cquery.json').exists()
        assert 'Unrecognized option: --qualification_missing_option' in (TMP / 'invalid-option-build.err').read_text()
    phase('invalid_build_option', rejected_options, result)
    result['execution_finished'] = now()
    server.shutdown()
    assert git('status', '--porcelain', '--untracked-files=no') == ''
    expected = {'amd64-pkg', 'amd64-runsc', 'arm64-pkg', 'arm64-runsc', 'invalid_build_option'}
    assert result['phases'].keys() == expected
    result['status'] = 'PASS' if all(row['status'] == 'PASS' for row in result['phases'].values()) else 'FAIL'


if __name__ == '__main__':
    if Path(sys.argv[0]).name in ('bazel', 'docker', 'gcloud'):
        raise SystemExit(wrapped())
    TMP.mkdir(exist_ok=True)
    result = {'source': SOURCE, 'package': PKG, 'staged': STAGED, 'status': 'FAIL', 'phases': {}}
    try:
        main(result)
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'[:2000]
    finally:
        result['verified_at'] = now()
        write(TMP / 'result.json', result)
    raise SystemExit(0 if result['status'] == 'PASS' else 1)
