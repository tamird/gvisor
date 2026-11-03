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
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from threading import Thread
import time
import xml.etree.ElementTree as ET
from zipfile import ZipFile


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
    if cmd == 'build' and os.environ.get('CHECK_ANALYSIS', '1') == '1':
        flags.append('--nobuild')
    start = time.monotonic()
    with (TMP / f'{arm}-{cmd}.out').open('wb') as out, (TMP / f'{arm}-{cmd}.err').open('wb') as err:
        p = subprocess.run([os.environ['REAL_BAZEL'], '--host_jvm_args=-Xmx4g', cmd, *flags, *args], stdout=out, stderr=err)
    write(TMP / f'{arm}-{cmd}.json', {'args': args, 'exit': p.returncode, 'seconds': time.monotonic() - start})
    if p.returncode:
        for line in (TMP / f'{arm}-{cmd}.err').read_text(errors='replace').splitlines():
            if line.startswith('ERROR:') and not re.search(r'https?://|token|secret|authorization|api.key', line, re.I):
                print(line[:800], file=sys.stderr)
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
    import yaml

    doc = yaml.safe_load(git('show', f'{rev}:{file}'))
    step, = [s for s in doc['steps'] if isinstance(s, dict) and s.get('label') == label]
    assert step['env']['DOCKER_BUILD'] == 'false', step
    return step


def execute(arm, cmd, env):
    env = env | {'CHECK_ARM': arm}
    p = subprocess.run(cmd, env=env, capture_output=True, text=True)
    (TMP / f'{arm}-entry.out').write_text(p.stdout)
    (TMP / f'{arm}-entry.err').write_text(p.stderr)
    row = {'command': cmd, 'exit': p.returncode, 'finished_at': now()}
    if p.returncode:
        row['diagnostics'] = [line[:800] for line in p.stderr.splitlines() if line.startswith('ERROR:')][:8]
    write(TMP / f'{arm}-entry.json', row)
    assert not (TMP / f'{arm}-docker.json').exists(), arm
    return row


def producer(arm, env, target):
    args = json.loads((TMP / f'{arm}-build.json').read_text())['args']
    assert args[-1] in ('//pkg/...', '//runsc/...'), args
    args = args[:-1]
    if args[-1] == '--':
        args.pop()
    graph = json.loads(call([env['REAL_BAZEL'], '--host_jvm_args=-Xmx4g', 'aquery', *args, '--output=jsonproto', '--noinclude_artifacts', target], env))
    labels = {t['id']: t['label'].removeprefix('@@') for t in graph['targets']}
    actions = sorted((labels[a['targetId']], a['mnemonic'], a['actionKey']) for a in graph['actions'] if a['mnemonic'] in ('GoLink', 'GoCompilePkg'))
    assert actions and all(row[0] == target for row in actions), actions
    return actions


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
            current['selected_producer_actions'] = producer(arm, env, target)
        except Exception as exc:
            failures.append(arm)
            row.setdefault(arm, {})['error'] = f'{type(exc).__name__}: {exc}'[:1000]
    assert not failures, failures
    left, right = row[key + '-old'], row[key + '-make']
    a, b = partitions(left['configured']), partitions(right['configured'])
    assert sorted(a.values()) == sorted(b.values()), key
    row['configuration_correspondence'] = {old_id: [new_id for new_id, members in b.items() if members == owners] for old_id, owners in a.items()}
    assert all(len(matches) == 1 for matches in row['configuration_correspondence'].values()), row
    row['action_query'] = action_observations(left, right)


def action_observations(left, right):
    # The projection omitted action configurations. Do not treat every loaded
    # instance of the selected label as the top-level configured target.
    old = {tuple(action) for action in left['selected_producer_actions']}
    new = {tuple(action) for action in right['selected_producer_actions']}
    assert old and new and old <= new, (old, new)
    assert all(re.fullmatch('[0-9a-f]{64}', key) for _, _, key in old | new)
    return {
        'old_keys': len(old),
        'new_keys': len(new),
        'all_old_keys_retained': True,
        'keys_equal': old == new,
        'complete_action_configuration_identity': 'UNVERIFIED',
    }


def reconcile(path, result):
    raw = path.read_bytes()
    digest = sha256(raw).hexdigest()
    assert digest == '63d97cf219788f9db6efb3e51861f82c4f5a441c6dc1f2f6f0ed02061f2968bb'
    with ZipFile(io.BytesIO(raw)) as archive_file:
        assert archive_file.namelist() == ['result.json']
        original = json.loads(archive_file.read('result.json'))
    result['original'] = original
    result['original_artifact_sha256'] = digest
    result['original_run'] = 41579868653
    result['original_artifact'] = 13205600447
    result['workload_repeated'] = False
    assert original['status'] == 'FAIL'
    assert (original['source'], original['package'], original['staged']) == (SOURCE, PKG, STAGED)
    assert git('diff', '--name-only', SOURCE, 'HEAD').splitlines() == [
        '.github/qualification/buildkite-client-options.py',
        '.github/workflows/buildkite-client-options.yml',
    ]
    phases = original['phases']
    pairs = ('amd64-pkg', 'amd64-runsc', 'arm64-pkg', 'arm64-runsc')
    assert set(phases) == {*pairs, 'retained_archives', 'staged_checker', 'invalid_build_option'}
    assert all(phases[key]['status'] == 'PASS' for key in ('retained_archives', 'staged_checker', 'invalid_build_option'))
    cache = phases['staged_checker']['complete']['bazel']['args'][2]
    assert re.fullmatch('--remote_cache=http://127[.]0[.]0[.]1:[0-9]+', cache), cache

    def forwarded(record, present):
        assert record['agent_cache_received'] == record['quoted_option_received'] == present
        assert (cache in record['args']) == (META in record['args']) == present

    result['configured_commands'] = {}
    for key in pairs:
        phase_record = phases[key]
        expected_status = 'PASS' if key == 'arm64-runsc' else 'FAIL'
        assert phase_record['status'] == expected_status
        if expected_status == 'FAIL':
            assert phase_record['error'] == f'AssertionError: {key}'
        old, new = (phase_record[key + suffix] for suffix in ('-old', '-make'))
        assert old['exit'] == old['build']['exit'] == new['exit'] == new['build']['exit'] == new['cquery']['exit'] == 0
        forwarded(old['build'], False)
        forwarded(new['build'], True)
        forwarded(new['cquery'], True)
        assert old['configured'] == new['configured']
        assert len(old['configured']) == (1951 if key.endswith('pkg') else 391)
        groups = partitions(old['configured'])
        assert phase_record['configuration_correspondence'] == {identity: [identity] for identity in groups}
        assert new['cquery']['enumerated_outputs'] > 0 and new['cquery']['materialized_outputs_claimed'] is False
        result['configured_commands'][key] = {
            'status': 'PASS',
            'configured_owners': len(old['configured']),
            'configuration_ids': sorted(groups),
            'output_descriptors': new['cquery']['enumerated_outputs'],
            'action_query': action_observations(old, new),
        }
    checker = phases['staged_checker']
    expected_exits = {'old': (0, 0), 'complete': (0, 0), 'missing': (2, 1), 'corrupt': (2, 1), 'truncated': (2, 1), 'download': (2, 0)}
    for name, exits in expected_exits.items():
        record = checker[name]
        assert (record['exit'], record['bazel']['exit']) == exits, (name, record)
        forwarded(record['bazel'], name != 'old')
    assert checker['old']['fixture_sha256'] == checker['complete']['fixture_sha256'] == checker['download']['fixture_sha256']
    assert checker['missing']['diagnostic'] == 'containerd-shim-runsc-v1'
    assert checker['corrupt']['diagnostic'] == 'checksum'
    assert checker['truncated']['diagnostic'] == 'unexpected EOF'
    assert checker['empty']['exit'] == 0
    assert phases['invalid_build_option']['exit'] == phases['invalid_build_option']['build']['exit'] == 2
    result['scope'] = 'Actual command option forwarding, configured target sets, cquery descriptors and checker stdin/failure behavior; no package build or complete producer action-set claim.'
    result['status'] = 'PASS'


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
    phase('retained_archives', previous, result)
    phase('staged_checker', lambda row: staged(env, row), result)
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
    result['status'] = 'PASS' if all(row['status'] == 'PASS' for row in result['phases'].values()) else 'FAIL'


def plugin(result):
    """Observe the plugin through the same command/options boundary."""
    source = '0ec4753b7f28212e1faed1145d29d763f59e52f4'
    review = '3d576b2ffb5b3dce0e3ebc7651338d1de66f0e3a'
    original = 'a490e1c0967c8b232b45670b8eb876b9cddaae0c'
    target = '//runsc:runsc-plugin-stack'
    cross = '--extra_toolchains=@llvm//toolchain:linux_x86_64_to_linux_aarch64'
    result.update(source=source, review=review, original=original)
    assert (platform.system(), platform.machine()) == ('Linux', 'x86_64')
    assert git('rev-parse', 'HEAD') == source
    assert git('rev-parse', 'HEAD^') == original
    assert git('status', '--porcelain', '--untracked-files=no') == ''
    for name in ('.bazelrc', 'tools/bazel.mk', '.buildkite/hooks/pre-command', '.buildkite/hooks/post-command'):
        assert git('rev-parse', f'HEAD:{name}') == git('rev-parse', f'{review}:{name}'), name
    recipes = [re.search(r'^runsc-plugin-stack:\n(?:\t.*\n)+', git('show', ref + ':Makefile'), re.M)[0]
               for ref in (source, review)]
    assert recipes[0] == recipes[1]
    assert git('diff', '--name-only', original, source).splitlines() == ['.bazelrc', '.buildkite/pipeline.yaml', 'tools/bazel.mk']
    label = ':speedboat: Compile runsc-plugin-stack (AMD64)'
    old, new = pipeline(original, '.buildkite/pipeline.yaml', label), pipeline(source, '.buildkite/pipeline.yaml', label)
    assert new == pipeline(review, '.buildkite/pipeline.yaml', label)
    assert old['agents'] == new['agents'] == {'arch': 'amd64', 'queue': 'default'}
    assert new['command'] == 'make runsc-plugin-stack'
    assert shlex.split(old['command']) == ['bazel', 'build', '-c', 'opt', '--config=plugin-tldk', cross, target]
    real = shutil.which('bazel'); assert real
    assert call([real, '--version']).strip() == 'bazel ' + Path('.bazelversion').read_text().strip() == 'bazel 8.8.1'
    bindir = TMP / 'bin'; bindir.mkdir()
    for name in ('bazel', 'docker'):
        (bindir / name).symlink_to(Path(__file__).resolve())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Cache)
    Thread(target=server.serve_forever, daemon=True).start()
    env = os.environ | {'REAL_BAZEL': real, 'PATH': str(bindir) + ':' + os.environ['PATH'],
                        'DOCKER_BUILD': 'false', 'DOCKER_HOST': 'unix:///absent-buildkite-docker',
                        'BAZEL_REMOTE_CACHE': f'--remote_cache=http://127.0.0.1:{server.server_port}',
                        'BAZEL_OPTIONS': shlex.quote(META) + ' --noremote_upload_local_results'}
    result['host'] = {'architecture': platform.machine(), 'kernel': platform.release(),
                      'gcc': call(['gcc', '--version']).splitlines()[0],
                      'arm_gcc': shutil.which('aarch64-linux-gnu-gcc')}

    def retained(row):
        data = (Path(os.environ['RUNNER_TEMP']) / 'plugin-original.zip').read_bytes()
        assert sha256(data).hexdigest() == 'cda8c24f8e12f0223567c7ab48e0d9bde90db223559ebc0a85b02d7974197fd2'
        with ZipFile(io.BytesIO(data)) as archive_file:
            assert archive_file.namelist() == ['result.json']
            receipt = json.loads(archive_file.read('result.json'))
        assert receipt['status'] == 'PASS' and receipt['source'] == 'b8af597dce91b33e5753b450774be07f1d3b92e3'
        assert receipt['effective_command'] == shlex.split(old['command'])
        assert receipt['defined_plugin_preinitstack'] and receipt['product']['sha256'] == 'c34e90762ce6ac34a7ed9977118df1b100f9d3d6de59d6bd4a970bdd05868c83'
        row.update(run=41395191771, artifact=13119417233, zip_sha256=sha256(data).hexdigest(),
                   source=receipt['source'], product=receipt['product'], build=receipt['build_verdict'],
                   reused_execution=True, original_binary_bytes_retrieved=False)

    def toolchain(row):
        raw = call([real, 'query', '@llvm//toolchain:linux_x86_64_to_linux_aarch64', '--output=xml'], env)
        rule, = ET.fromstring(raw).findall('rule')
        assert rule.attrib['class'] == 'toolchain'
        constraints = {key: [n.attrib['value'] for n in rule.findall(f"list[@name='{key}']/label")]
                       for key in ('exec_compatible_with', 'target_compatible_with')}
        assert {s.split('//', 1)[1] for s in constraints['exec_compatible_with']} == {'cpu:x86_64', 'os:linux'}, constraints
        assert {s.split('//', 1)[1] for s in constraints['target_compatible_with']} == {'cpu:aarch64', 'os:linux'}, constraints
        row.update(label=rule.attrib['name'], constraints=constraints, query_sha256=sha256(raw.encode()).hexdigest())

    def actions(args):
        flags = args[:-1]
        assert args[-1] == target
        graph = json.loads(call([real, '--host_jvm_args=-Xmx4g', 'aquery', *flags,
                                'mnemonic("GoStdlib|GoLink|ForeignCc", deps(' + target + '))',
                                '--output=jsonproto', '--noinclude_artifacts'], env))
        labels = {t['id']: t['label'] for t in graph['targets']}
        configs = {c['id']: c['checksum'] for c in graph['configuration']}
        observed = []
        for action in graph['actions']:
            values = {v['key']: v.get('value', '') for v in action.get('environmentVariables', [])}
            observed.append({'owner': labels[action['targetId']], 'mnemonic': action['mnemonic'],
                             'configuration': configs[action['configurationId']], 'key': action['actionKey'],
                             'GOARCH': values.get('GOARCH'), 'CC': values.get('CC'), 'CGO_ENABLED': values.get('CGO_ENABLED')})
        assert observed and all(re.fullmatch('[0-9a-f]{64}', r['key']) for r in observed)
        return sorted(observed, key=lambda r: (r['owner'], r['mnemonic'], r['configuration'], r['key']))

    def command_phase(row, arm, command, options):
        row.update(execute(arm, shlex.split(command), env))
        row['build'] = command_record(arm, 'build', env, options)
        assert row['exit'] == row['build']['exit'] == 0, row
        row['configured'] = configured(arm)
        row['actions'] = actions(row['build']['args'])
        if options:
            row['cquery'] = command_record(arm, 'cquery', env, True)
            assert row['cquery']['exit'] == 0
        for command_name in ('build', 'cquery') if options else ('build',):
            args = row[command_name]['args']
            assert args.count(env['BAZEL_REMOTE_CACHE']) == args.count(META) == int(options)

    def old_analysis(row):
        rc = Path('.bazelrc'); saved = rc.read_bytes()
        try:
            rc.write_bytes(subprocess.check_output(['git', 'show', original + ':.bazelrc']))
            command_phase(row, 'plugin-old', old['command'], False)
        finally:
            rc.write_bytes(saved)

    def compare(row):
        a, b = result['phases']['old_analysis'], result['phases']['make_analysis']
        assert a['status'] == b['status'] == 'PASS'
        assert a['configured'] == b['configured'], (a['configured'], b['configured'])
        assert a['actions'] == b['actions'], 'Selected action/configuration identities changed'
        stdlib = [r for r in b['actions'] if r['mnemonic'] == 'GoStdlib' and r['CGO_ENABLED'] == '1']
        assert {r['GOARCH'] for r in stdlib} == {'amd64', 'arm64'}, stdlib
        assert all('llvm' in r['CC'] if r['GOARCH'] == 'arm64' else r['CC'] == '/usr/bin/x86_64-linux-gnu-gcc' for r in stdlib)
        row.update(configured=a['configured'], actions=a['actions'], exact_action_configurations_equal=True)

    def materialize(row):
        row.update(execute('plugin-built', shlex.split(new['command']), env | {'CHECK_ANALYSIS': '0'}))
        for cmd in ('build', 'cquery'):
            row[cmd] = command_record('plugin-built', cmd, env, True)
            assert row[cmd]['exit'] == 0 and row[cmd]['args'].count(META) == row[cmd]['args'].count(env['BAZEL_REMOTE_CACHE']) == 1
        assert row['exit'] == 0
        completed = [e['id']['targetCompleted'] for e in events('plugin-built', 'build')
                     if 'targetCompleted' in e['id'] and e.get('completed', {}).get('success')]
        assert any(r['label'] == target and not r.get('aspect') for r in completed)
        row['completed_targets'] = completed
        pairs = [line.split() for line in (TMP / 'plugin-built-cquery.out').read_text().splitlines() if line.strip()]
        (relative, destination), = pairs
        elf = Path(relative); data = elf.read_bytes()
        assert data[:6] == b'\x7fELF\x02\x01' and int.from_bytes(data[18:20], 'little') == 62
        description = call(['readelf', '-hlWd', str(elf)])
        assert not re.search(r'^\s*INTERP\s|\(NEEDED\)', description, re.M)
        version = call([str(elf.resolve()), '--version']).strip()
        assert version.startswith('runsc version ') and '\nspec: ' in version
        assert any(line.endswith(' T plugin_preinitstack') for line in call(['nm', '--defined-only', str(elf)]).splitlines())
        stdout = (TMP / 'plugin-built-entry.out').read_text().splitlines()
        assert str(elf.resolve()) in stdout, stdout
        row['product'] = {'path': relative, 'destination': destination, 'sha256': sha256(data).hexdigest(),
                          'size': len(data), 'e_machine': 62, 'PT_INTERP': False, 'DT_NEEDED': [],
                          'plugin_preinitstack': True, 'version': version}

    def invalid(row):
        bad = env | {'BAZEL_OPTIONS': env['BAZEL_OPTIONS'] + ' --qualification_missing_option'}
        row.update(execute('plugin-invalid', shlex.split(new['command']), bad))
        row['build'] = json.loads((TMP / 'plugin-invalid-build.json').read_text())
        assert row['exit'] == row['build']['exit'] == 2
        assert not (TMP / 'plugin-invalid-cquery.json').exists()
        assert 'Unrecognized option: --qualification_missing_option' in (TMP / 'plugin-invalid-build.err').read_text()

    phase('retained_output', retained, result)
    phase('toolchain_constraints', toolchain, result)
    phase('old_analysis', old_analysis, result)
    phase('make_analysis', lambda row: command_phase(row, 'plugin-make', new['command'], True), result)
    phase('selected_actions', compare, result)
    phase('native_make_build', materialize, result)
    phase('invalid_option', invalid, result)
    server.shutdown()
    assert git('status', '--porcelain', '--untracked-files=no') == ''
    result['execution_finished'] = now()
    result['status'] = 'PASS' if all(row['status'] == 'PASS' for row in result['phases'].values()) else 'FAIL'


if __name__ == '__main__':
    if Path(sys.argv[0]).name in ('bazel', 'docker', 'gcloud'):
        raise SystemExit(wrapped())
    TMP.mkdir(exist_ok=True)
    result = {'source': SOURCE, 'package': PKG, 'staged': STAGED, 'status': 'FAIL', 'phases': {}}
    try:
        if sys.argv[1:] == ['--plugin']:
            plugin(result)
        elif sys.argv[1:]:
            mode, archive_path = sys.argv[1:]
            assert mode == '--reconcile-original', mode
            reconcile(Path(archive_path), result)
        else:
            main(result)
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'[:2000]
    finally:
        result['verified_at'] = now()
        write(TMP / 'result.json', result)
    raise SystemExit(0 if result['status'] == 'PASS' else 1)
