#!/usr/bin/env python3
"""Exercise the native container Make selection in the maintained KVM guest."""
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

from credential_artifacts import prepare
from qualification_report import xml_cases
from unit_matrix import configured_targets

TMP = Path(os.environ['RUNNER_TEMP'])
SOURCE = os.environ['SOURCE']
PRODUCT = os.environ['PRODUCT']
CARRIER = os.environ['GITHUB_SHA']
GUEST = '38745c527784376e0b5d915fe5ebd85e1d1385fd'
PKG = '.github/qualification/unit-cgroup-v1'
NAMES = ('container_test', 'boot_test', 'gofer_env_test', 'gofer_maxprocs_test', 'modify_test', 'serialization_test')
OWNERS = {'//runsc/container:' + name for name in NAMES}
SHARDS = {name: 8 if name == 'container_test' else 1 for name in NAMES}
META = '--build_metadata=NATIVE_CONTAINER=Buildkite container'
CACHE = '--remote_cache=grpcs://remote.buildbuddy.io'
RESULT = TMP / 'container-native-result.json'
DOC = dict(status='INCOMPLETE', product=PRODUCT, source=SOURCE, carrier=CARRIER, guest=GUEST,
           phases={}, commands={}, payloads={}, attempts=[], modes={}, scope='native Make analysis and unchanged native binaries in KVM')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def save():
    DOC['verdict_utc'] = now()
    data = (json.dumps(DOC, indent=2) + '\n').encode()
    require(len(data) < 2 * 1024 * 1024, 'receipt too large')
    require(not re.search(rb'(?i)(?:gh[pousr]_[a-z0-9]{20}|github_pat_|(?:authorization|x-buildbuddy-api-key)\s*[:=]|Bearer\s+[a-z0-9])', data), 'publication scan')
    RESULT.write_bytes(data)


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def execute(name, args, *, env=None, timeout=2400):
    row = dict(started_utc=now(), exit=None)
    DOC['commands'][name] = row
    save()
    log = TMP / (name + '.log')
    with log.open('wb') as stream:
        try:
            row['exit'] = subprocess.run(args, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            row['exit'] = 124
    row['finished_utc'] = now()
    content = log.read_bytes()
    row['log'] = dict(sha256=sha(content), bytes=len(content))
    row['diagnostic_classes'] = [kind for kind, pattern in {
        'fetch': rb'(?i)error downloading|failed to fetch', 'compiler': rb'(?i)fatal error:|undefined reference',
        'disk': rb'(?i)no space left', 'permission': rb'(?i)permission denied|operation not permitted',
        'test_failure': rb'(?i)tests fail|FAILED:|--- FAIL:',
    }.items() if re.search(pattern, content)]
    save()
    return row['exit']


def phase(name, action):
    DOC['phases'][name] = dict(status='INCOMPLETE', started_utc=now())
    save()
    try:
        action()
        DOC['phases'][name]['status'] = 'PASS'
    except Exception as exc:
        DOC['phases'][name].update(error_class=type(exc).__name__)
        # Only our fixed assertion messages are safe to publish.
        if type(exc) is ValueError and not any(c in str(exc) for c in ('\n', '{', '<')):
            DOC['phases'][name]['error'] = str(exc)[:240]
    DOC['phases'][name]['finished_utc'] = now()
    save()


def native(rc):
    require(git('rev-parse', 'HEAD') == SOURCE, 'wrong product integration')
    require(not git('diff', '--name-only', 'HEAD'), 'dirty product integration')
    require(git('rev-parse', SOURCE + '^') == PRODUCT, 'wrong compiler integration parent')
    DOC['bazel_version'] = subprocess.check_output(['bazel', '--version'], text=True).strip()
    require(DOC['bazel_version'] == 'bazel ' + Path('.bazelversion').read_text().strip(), 'wrong Bazel')
    bazel = ['bazel', '--bazelrc=' + str(rc)]
    for mode in ('false', 'true'):
        name = 'make-' + mode
        bep = TMP / (name + '.bep')
        env = dict(os.environ, DOCKER_BUILD='false', CGROUPV2=mode, RUNTIME='buildkite_runtime',
                   BAZEL=shlex.join(bazel), BAZEL_REMOTE_CACHE=CACHE,
                   BAZEL_OPTIONS=shlex.join(['--config=x86_64', '--jobs=4', '--nobuild', META,
                                             '--build_event_json_file=' + str(bep)]))
        code = execute(name, ['make', 'container-tests', 'CGROUPV2=' + mode,
                             'BAZEL=' + shlex.join(bazel)], env=env, timeout=900)
        # Make supplies its own later BEP argument. Preserve its actual output.
        actual = Path('.build_events.json')
        if actual.exists():
            shutil.copyfile(actual, bep)
        rows = events(bep)
        options = next(e['optionsParsed']['explicitCmdLine'] for e in rows if 'optionsParsed' in e)
        bazel_exit = next(e['finished']['exitCode'].get('code', 0) for e in rows if 'finished' in e)
        selected = configured_targets(str(bep), tests=True, allow_test_no_build=True, expected_exit=bazel_exit)
        filtered = {label: target for label, target in selected.items() if not {'manual', 'nogo'} & set(target.tags)}
        DOC['modes'][mode] = dict(analysis_exit=code, bazel_exit=bazel_exit, owners=sorted(filtered),
            configured=[dict(label=label, configuration=target.configuration, tags=target.tags) for label, target in sorted(selected.items())],
            cgroup_option='--test_env=CGROUPV2=' + mode in options,
            cache_count=options.count(CACHE), quoted_option_count=options.count(META), analysis_only=True)
        save()
        require((code, bazel_exit) in ((0, 0), (2, 1)) and set(filtered) == OWNERS, 'Make configured owner mismatch')
        DOC['commands'][name]['disposition'] = 'VALIDATED_ANALYSIS_ONLY'
        require(DOC['modes'][mode]['cgroup_option'] and options.count(CACHE) == options.count(META) == 1,
                'Make lost cgroup/cache/quoted option')
        require(not any('testResult' in e for e in rows), 'analysis unexpectedly ran tests')
    # The native Make test command selects these exact six executables. Only
    # declare archive outputs; do not modify test source, arguments or runfiles.
    build = Path('runsc/container/BUILD')
    original = build.read_text()
    build.write_text('load("@rules_pkg//pkg:pkg.bzl", _qualification_tar = "pkg_tar")\n' + original + '\n' +
        '\n'.join('_qualification_tar(name = "%s_capsule", testonly = True, srcs = [":%s"], include_runfiles = True, strip_prefix = "/", allow_duplicates_with_different_content = False)' % (n, n) for n in NAMES))
    flags = ['--config=x86_64', '--strip=never', '--jobs=6', CACHE, '--remote_executor=', '--keep_going']
    targets = ['//runsc/container:' + n + '_capsule' for n in NAMES]
    try:
        code = execute('native-build', bazel + ['build', *flags, '--build_event_json_file=' + str(TMP / 'native-build.bep'), *targets])
        require(code == 0, 'native capsule build failed')
        for name in NAMES:
            path = Path('bazel-bin/runsc/container/' + name + '_capsule.tar')
            require(path.stat().st_size < 1024 ** 3, 'capsule size bound')
            dest = TMP / (name + '.tar')
            shutil.copyfile(path, dest)
            with tarfile.open(dest) as archive:
                members = archive.getmembers()
                require(len(members) < 32768 and sum(m.size for m in members) < 1024 ** 3, 'capsule member bound')
                require(all(not m.name.startswith('/') and '..' not in Path(m.name).parts for m in members), 'unsafe capsule path')
                candidates = [m for m in members if Path(m.name).name == name and '.runfiles/' not in m.name and (m.isfile() or m.islnk())]
                require(len(candidates) == 1, 'ambiguous capsule executable')
                entry = candidates[0]
                binary = archive.extractfile(entry).read()
                require(binary[:6] == b'\x7fELF\x02\x01' and int.from_bytes(binary[18:20], 'little') == 62, 'non-native test ELF')
                runfiles = entry.name + '.runfiles'
                require(any(m.name.startswith(runfiles + '/_main/') for m in members), 'missing capsule runfiles')
                release = [m for m in members if m.name == runfiles + '/_main/release/runsc']
                if name in ('container_test', 'boot_test'):
                    require(len(release) == 1, 'missing declared runsc')
                    runsc = archive.extractfile(release[0]).read()
                    runsc_hash = sha(runsc)
                else:
                    runsc_hash = None
            DOC['payloads'][name] = dict(sha256=sha(dest.read_bytes()), bytes=dest.stat().st_size,
                binary=entry.name, runfiles=runfiles, elf_sha256=sha(binary), elf_bytes=len(binary), runsc_sha256=runsc_hash)
            save()
    finally:
        build.write_text(original)
    require(not git('diff', '--name-only', 'HEAD'), 'product source changed')


def guest(rc):
    git('switch', '--quiet', '--detach', CARRIER)
    require(git('rev-parse', 'HEAD~2') == GUEST, 'guest source mismatch')
    package = Path(PKG)
    for name, row in DOC['payloads'].items():
        shutil.copyfile(TMP / (name + '.tar'), package / (name + '.tar'))
        require(sha((package / (name + '.tar')).read_bytes()) == row['sha256'], 'capsule changed')
    (package / 'inputs.json').write_text(json.dumps(DOC['payloads']))
    # Shell-readable paths/hashes are source-derived and restricted before writing.
    for name, row in DOC['payloads'].items():
        require(all(re.fullmatch(r'[A-Za-z0-9_./+-]+', row[key]) for key in ('binary', 'runfiles')), 'invalid archive path')
        (package / (name + '.inputs')).write_text('\n'.join(row[key] or '-' for key in ('sha256', 'binary', 'runfiles', 'elf_sha256', 'runsc_sha256')) + '\n')
    labels = ['//' + PKG + ':' + mode + '_' + n for mode in ('v1', 'v2') for n in NAMES]
    bep = TMP / 'guest.bep'
    args = ['bazel', '--bazelrc=' + str(rc), 'test', '--config=rbe', '--config=rbe-local-tests',
            '--//tools/bazeldefs:local_test_architecture=amd64', '--jobs=80', '--local_test_jobs=2',
            '--nocache_test_results', '--flaky_test_attempts=1', '--runs_per_test=1', '--keep_going',
            '--zip_undeclared_test_outputs', '--nobuild_event_json_file_path_conversion', '--test_timeout=900', '--test_output=errors',
            '--build_event_json_file=' + str(bep), *labels]
    code = execute('guest', args, timeout=3600)
    DOC['execution_finished_utc'] = now()
    save()
    rows = events(bep)
    DOC['guest_invocation'] = next(e['started']['uuid'] for e in rows if 'started' in e)
    seen = set()
    leaves_by_owner = {}
    passed = {mode: set() for mode in ('v1', 'v2')}
    for event in rows:
        if 'testResult' not in event:
            continue
        ident, result = event['id']['testResult'], event['testResult']
        label = ident['label']
        require(label in labels, 'unexpected guest owner')
        mode, name = label.split(':')[1].split('_', 1)
        shard = int(ident.get('shard', 0))
        key = (mode, name, shard)
        require(key not in seen and shard in range(SHARDS[name]), 'duplicate or wrong shard')
        seen.add(key)
        row = dict(owner='//runsc/container:' + name, mode=mode, shard=shard,
            status=result['status'], local_cache=bool(result.get('cachedLocally')),
            remote_cache=bool(result.get('executionInfo', {}).get('cachedRemotely')),
            strategy=result.get('executionInfo', {}).get('strategy'),
            start_ms=result.get('testAttemptStartMillis'), duration_ms=result.get('testAttemptDurationMillis'))
        DOC['attempts'].append(row)
        save()
        try:
            outputs = {f['name']: f['uri'] for f in result.get('testActionOutput', []) if 'uri' in f}
            def local_file(name):
                uri = urlsplit(outputs[name])
                require(uri.scheme == 'file' and uri.netloc in ('', 'localhost'), 'nonlocal guest report')
                return Path(unquote(uri.path))
            xml = local_file('test.xml')
            require(xml.stat().st_size < 16 * 1024 * 1024, 'XML size bound')
            count = xml_cases(xml)
            cases = list(ET.parse(xml).iter('testcase'))
            def safe_name(value):
                return value if re.fullmatch(r'[A-Za-z0-9_./=:+, ()\[\]-]{1,400}', value) else 'sha256:' + sha(value.encode())
            def skipped_case(case):
                child = case.find('skipped')
                detail = ''.join(child.itertext()) if child is not None else ''
                reasons = [message for message in (
                    'Checkpoint not supported', 'TSC adjustment is only supported on KVM',
                    "PID 1's oom_score_adj is writable", 'kernel >=6.3 is required for id mapped mount tests',
                    'Skipping test when running with race detector (gotsan)', 'This test is only for shared bind mounts',
                    'not available to bind mount',
                ) if message in detail]
                reasons.extend(tool + ' unavailable' for tool in ('mkfs.erofs', 'busybox') if tool + ' is not available' in detail)
                return dict(name=safe_name(case.get('name', '')), reasons=reasons,
                            reason_sha256=sha(detail.encode()), status=case.get('status'), result=case.get('result'))
            row['xml'] = dict(sha256=sha(xml.read_bytes()), passed=count.get('passed', 0), failed=count.get('failed', 0),
                skipped=count.get('skipped', 0), failed_cases=[safe_name(c.get('name', '')) for c in cases if c.find('failure') is not None or c.find('error') is not None],
                skipped_cases=[skipped_case(c) for c in cases if c.find('skipped') is not None or c.get('status') == 'notrun' or c.get('result') == 'suppressed'])
            names = {c.get('name', '') for c in cases}
            parents = {'/'.join(n.split('/')[:i]) for n in names for i in range(1, len(n.split('/')))}
            leaves = {n for n in names - parents if n.startswith('Test')}
            earlier = leaves_by_owner.setdefault((mode, name), set())
            require(leaves and not earlier & leaves, 'duplicate or empty leaf census across shards')
            earlier.update(leaves)
            row['xml']['leaf_cases'] = sorted(map(safe_name, leaves))
            row['xml']['case_names_sha256'] = sha(('\n'.join(sorted(names)) + '\n').encode())
            passed[mode].update(c.get('name', '') for c in cases if not any(c.find(k) is not None for k in ('failure', 'error', 'skipped')) and c.get('status') != 'notrun' and c.get('result') != 'suppressed')
            archive_path = local_file('test.outputs__outputs.zip')
            with zipfile.ZipFile(archive_path) as archive:
                info = archive.getinfo('guest/outputs/proof')
                require(info.file_size < 4096, 'guest proof size')
                proof = archive.read(info).decode().splitlines()
            require(proof[:5] == [mode, DOC['payloads'][name]['sha256'], DOC['payloads'][name]['elf_sha256'], 'docker_absent', 'kvm_present'], 'guest identity mismatch')
            require(len(proof) == 12 and proof[5] == 'CGROUPV2=' + ('false' if mode == 'v1' else 'true') and proof[6] == '6.8.0-138-generic' and all(re.fullmatch(r'[0-9a-f]{64}', h) for h in (proof[7], proof[8], proof[11])), 'guest capability proof mismatch')
            require(proof[9:11] == [str(shard), str(SHARDS[name])], 'guest shard environment mismatch')
            row['guest_proof'] = dict(cgroup_v2=mode == 'v2', kernel=proof[6], mkfs_sha256=proof[7], loader_sha256=proof[8], erofs_archive_sha256=proof[11], shard_index=int(proof[9]), total_shards=int(proof[10]), docker_absent=True, kvm_present=True)
            save()
        except Exception as exc:
            row['report_error_class'] = type(exc).__name__
            save()
    expected = {(mode, n, s) for mode in ('v1', 'v2') for n in NAMES for s in range(SHARDS[n])}
    DOC['missing_attempts'] = [list(v) for v in sorted(expected - seen)]
    DOC['platform_cases'] = {mode: sorted(n for n in names if n in ('TestMinimalBoot/systrap', 'TestMinimalBoot/kvm', 'TestMinimalBoot/systrap_overlay', 'TestMinimalBoot/kvm_overlay', 'TestMountEROFS', 'TestMountEROFSConfig/image1')) for mode, names in passed.items()}
    save()
    require(seen == expected, 'incomplete guest attempts')
    require(all(not r['local_cache'] and not r['remote_cache'] and r['strategy'] == 'local' for r in DOC['attempts']), 'guest placement mismatch')
    require(all('xml' in r and 'guest_proof' in r for r in DOC['attempts']), 'incomplete guest reports')
    require(code == 0 and all(r['status'] == 'PASSED' and r['xml']['failed'] == 0 and r['xml']['passed'] > 0 for r in DOC['attempts']), 'guest functional failure')
    require(all({'TestMinimalBoot/systrap', 'TestMinimalBoot/kvm', 'TestMountEROFS', 'TestMountEROFSConfig/image1'} <= names for names in passed.values()), 'required platform or EROFS case did not pass')


def main():
    original = (TMP / 'original.zip').read_bytes()
    require(sha(original) == 'c61979d846b430d8744189a46ab54274e954f31f24f3499fc94eff285977b917', 'original ZIP mismatch')
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        require(archive.namelist() == ['container-native-result.json'], 'original members mismatch')
        prior = json.loads(archive.read('container-native-result.json'))
    require(prior['carrier'] == '77779c356c947e90fe486e1a6a8fcb36ad6368d9' and prior['status'] == 'INCOMPLETE'
            and not prior['commands'] and not prior['attempts'], 'unexpected original execution')
    DOC['original'] = dict(artifact=13248405646, zip_sha256=sha(original), receipt=prior, workload_repeated=False)
    DOC['host'] = dict(machine=os.uname().machine, kernel=os.uname().release, uid=os.getuid(),
                      caller_kvm_access=os.access('/dev/kvm', os.R_OK | os.W_OK))
    # The maintained local-root test route invokes QEMU through sudo. Check
    # that existing privilege boundary; never chmod the device or change policy.
    for permission in ('read', 'write'):
        DOC['host']['root_kvm_' + permission] = subprocess.run(
            ['sudo', '-n', 'test', '-r' if permission == 'read' else '-w', '/dev/kvm'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    save()
    require(DOC['host']['machine'] == 'x86_64' and DOC['host']['root_kvm_read'] and DOC['host']['root_kvm_write'], 'native root KVM unavailable')
    with tempfile.TemporaryDirectory(dir=TMP) as temporary:
        credential = Path(temporary)
        prepare(credential)
        os.environ.pop('BUILDBUDDY_API_KEY', None)
        rc = credential / 'client.bazelrc'
        rc.write_text('common --credential_helper=remote.buildbuddy.io=' + str(credential / 'helper') + '\n'
            'build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io\n'
            'build:buildbuddy_remote_executor --remote_cache=grpcs://remote.buildbuddy.io\n'
            'build:buildbuddy_remote_executor --bes_backend=grpcs://remote.buildbuddy.io\n'
            'build:buildbuddy_remote_executor --bes_results_url=https://app.buildbuddy.io/invocation/\n')
        git('switch', '--quiet', '--detach', SOURCE)
        phase('native_make_and_capsules', lambda: native(rc))
        if DOC['phases']['native_make_and_capsules']['status'] == 'PASS':
            phase('guest_v1_v2', lambda: guest(rc))
    if any(r['exit'] not in (0, None, 124) and r.get('disposition') != 'VALIDATED_ANALYSIS_ONLY' for r in DOC['commands'].values()):
        DOC['status'] = 'FAIL'
    elif set(DOC['phases']) == {'native_make_and_capsules', 'guest_v1_v2'} and all(r['status'] == 'PASS' for r in DOC['phases'].values()):
        DOC['status'] = 'PASS'
    save()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        DOC['error_class'] = type(exc).__name__
        save()
    sys.exit(0 if DOC['status'] == 'PASS' else 1)
