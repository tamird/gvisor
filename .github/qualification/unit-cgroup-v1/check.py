#!/usr/bin/env python3
"""Exercise the native container Make selection in the maintained KVM guest."""
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
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
SECRETS = [value for key, value in os.environ.items() if re.search('TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY', key) and len(value) > 16]
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
    require(not any(value.encode() in data or json.dumps(value)[1:-1].encode() in data for value in SECRETS), 'secret in receipt')
    require(not re.search(rb'(?i)(?:gh[pousr]_[a-z0-9]{20}|github_pat_|(?:authorization|x-buildbuddy-api-key)\s*[:=]|Bearer\s+[a-z0-9])', data), 'publication scan')
    RESULT.write_bytes(data)


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def diagnostic_lines(data):
    """Retain bounded compiler/packager errors, never commands or headers."""
    result = []
    for raw in data.decode(errors='replace').splitlines():
        if re.search(r'(?i)authorization|x-buildbuddy-api-key|remote_header|bes_header|github_pat_|gh[pousr]_', raw):
            continue
        line = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', raw)
        if not re.search(r'^(?:ERROR:|Error in |[A-Za-z]*(?:Error|Exception):|compilepkg:)|[.](?:go|cc|cpp|h|bzl):[0-9]+:', line):
            continue
        line = line.split(' command ', 1)[0]
        for secret in SECRETS:
            line = line.replace(secret, '[redacted]')
        line = line.replace(str(Path.cwd()) + '/', '')
        line = re.sub(r'/[^\s\"\x27]*/execroot/_main/', '', line)
        line = re.sub(r'(?:https?|grpcs?)://[^\s\"\x27]+', '[URL omitted]', line)
        result.append(line[:480])
        if len(result) == 16:
            break
    return result


def build_evidence(path):
    rows = events(path)
    result = dict(invocation=next((e['started']['uuid'] for e in rows if 'started' in e), None),
                  bep_sha256=sha(path.read_bytes()), failed_actions=[])
    DOC['native_build_evidence'] = result
    save()
    for event in rows:
        action = event.get('action')
        if not action or action.get('success', False):
            continue
        identity = event['id']['actionCompleted']
        owner = identity.get('label', '')
        require(re.fullmatch(r'(?:(?:@@?)[A-Za-z0-9_+~.-]+)?//[A-Za-z0-9_./:+-]+', owner), 'invalid failed owner')
        output = identity.get('primaryOutput')
        if output is not None:
            require(re.fullmatch(r'[A-Za-z0-9_./+@=-]{1,1024}', output)
                    and not output.startswith('/') and '..' not in Path(output).parts, 'invalid primary output')
        row = dict(owner=owner, configuration=identity.get('configuration', {}).get('id'),
                   exit=action.get('exitCode'), mnemonic=action.get('type'), primary_output=output, files=[])
        result['failed_actions'].append(row)
        save()
        for key in ('stderr', 'stdout'):
            item = action.get(key)
            if not item:
                continue
            uri = urlsplit(item.get('uri', ''))
            entry = dict(name=key, disposition='not_materialized')
            row['files'].append(entry)
            match = re.fullmatch(r'/(?:[^?#]*/)?blobs/([a-f0-9]{64})/(\d+)', uri.path)
            if uri.scheme != 'file':
                if uri.scheme == 'bytestream' and uri.netloc == 'remote.buildbuddy.io' and match:
                    entry.update(sha256=match[1], bytes=int(match[2]), disposition='CAS_only')
                save()
                continue
            log = Path(unquote(uri.path))
            try:
                if not log.resolve().is_relative_to(Path('bazel-out').resolve()):
                    entry['disposition'] = 'outside_output_root'
                elif log.stat().st_size >= 4 * 1024**2:
                    entry['disposition'] = 'over_size_bound'
                else:
                    data = log.read_bytes()
                    entry.update(disposition='read', sha256=sha(data), bytes=len(data), diagnostics=diagnostic_lines(data))
            except OSError as exc:
                entry.update(disposition='unavailable', error_class=type(exc).__name__)
            save()
        require(len(result['failed_actions']) <= 24, 'failed-action bound')


def execute(name, args, *, env=None, timeout=2400):
    row = dict(started_utc=now(), exit=None, kind='test' if name == 'guest' else 'build')
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
        'failure_marker': rb'(?i)tests fail|FAILED:|--- FAIL:',
    }.items() if re.search(pattern, content)]
    if row['exit']:
        row['diagnostics'] = diagnostic_lines(content)
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


def stdlib_evidence(path):
    from stdlib_provenance import summarize

    require(path.stat().st_size < 128 * 1024**2, 'execution log size bound')
    evidence = summarize(path, False)
    require(evidence['invocation'] == DOC['native_build_evidence']['invocation'], 'execution log invocation mismatch')
    require(0 < len(evidence['actions']) <= 64, 'stdlib action count bound')
    DOC['stdlib_producers'] = evidence
    save()
    producers = [r for r in evidence['actions'] if r['mnemonic'] == 'GoStdlib']
    analyses = [r for r in evidence['actions'] if r['mnemonic'] == 'GoStandardLibraryAnalysis']
    require(producers and analyses, 'missing stdlib producer or consumer')
    require(all(r['runner'] == 'processwrapper-sandbox' and not r['cacheHit'] and r['exit'] == 0 and not r['status'] for r in producers), 'stdlib producer placement or result mismatch')
    require(all(r['outputs'] and all(o['archiveCount'] > 0 and not o['excludedArchives'] for o in r['outputs']) for r in producers), 'undeclared or missing stdlib archives')
    require(all(r['exit'] == 0 and not r['status'] for r in analyses), 'stdlib analysis failed')


def native(rc):
    require(git('rev-parse', 'HEAD') == SOURCE, 'wrong product integration')
    require(not git('diff', '--name-only', 'HEAD'), 'dirty product integration')
    require(git('rev-parse', SOURCE + '^') == PRODUCT, 'wrong compiler integration parent')
    DOC['bazel_version'] = subprocess.check_output(['bazel', '--version'], text=True).strip()
    require(DOC['bazel_version'] == 'bazel ' + Path('.bazelversion').read_text().strip(), 'wrong Bazel')
    bazel = ['bazel', '--bazelrc=' + str(rc)]
    prior = DOC['original']['receipt']
    require(prior['bazel_version'] == DOC['bazel_version'], 'changed analysis client')
    for mode in ('false', 'true'):
        row = prior['modes'][mode]
        require(row['owners'] == sorted(OWNERS) and row['analysis_only'] and row['cgroup_option']
                and row['cache_count'] == row['quoted_option_count'] == 1
                and prior['commands']['make-' + mode]['disposition'] == 'VALIDATED_ANALYSIS_ONLY', 'invalid reused Make selection')
        DOC['modes'][mode] = row
    DOC['analysis_reused_from'] = '41671261499/13248882573'
    save()
    # The native Make test command selects these exact six executables. Only
    # declare archive outputs; do not modify test source, arguments or runfiles.
    build = Path('runsc/container/BUILD')
    original = build.read_text()
    build.write_text('load("@rules_pkg//pkg:pkg.bzl", _qualification_tar = "pkg_tar")\n' + original + '\n' +
        '\n'.join('_qualification_tar(name = "%s_capsule", testonly = True, srcs = [":%s"], include_runfiles = True, strip_prefix = "/", allow_duplicates_with_different_content = False)' % (n, n) for n in NAMES))
    # Constrain only the SDK producers to their declared files. An unrestricted
    # producer can compile packages intentionally omitted from sdk.srcs. Do not
    # reuse a remote result with that larger package set, or weaken Nogo's check.
    producer_flags = ['--strategy=GoStdlib=processwrapper-sandbox',
                      '--strategy=GoStdlibList=processwrapper-sandbox',
                      '--modify_execution_info=GoStdlib(List)?=+no-remote-cache']
    DOC['qualification_producer_options'] = producer_flags
    flags = ['--config=x86_64', '--strip=never', '--jobs=6', CACHE, '--remote_executor=', '--keep_going', *producer_flags]
    targets = ['//runsc/container:' + n + '_capsule' for n in NAMES]
    try:
        bep = TMP / 'native-build.bep'
        compact = TMP / 'native-build.compact.zst'
        code = execute('native-build', bazel + ['build', *flags, '--nobuild_event_json_file_path_conversion', '--build_event_json_file=' + str(bep), '--execution_log_compact_file=' + str(compact), *targets])
        if bep.exists():
            build_evidence(bep)
        # Keep execution and diagnostic collection independent on failure.
        phase('stdlib_producer_evidence', lambda: stdlib_evidence(compact))
        require(code == 0, 'native capsule build failed')
        require(DOC['phases']['stdlib_producer_evidence']['status'] == 'PASS', 'stdlib producer evidence incomplete')
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
    require(git('rev-parse', 'HEAD~5') == GUEST, 'guest source mismatch')
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


def recover_guests(setup_only=False):
    """Recover only the original guest attempts and bounded diagnostics."""
    from cas_inputs import blob, rpc

    raw = (TMP / 'original.zip').read_bytes()
    require(sha(raw) == '065c02f43387d0cdaa4c5417d2f89feec88202ebb9ca44c99d6225a1d4ce46e5', 'original ZIP mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.namelist() == ['container-native-result.json'], 'original members mismatch')
        prior = json.loads(archive.read('container-native-result.json'))
    require(prior['carrier'] == 'c90e30503332735ddb87d992588101661a7e2d1c'
            and prior['source'] == SOURCE and prior['product'] == PRODUCT and prior['status'] == 'FAIL'
            and prior['commands']['native-build']['exit'] == 0 and prior['commands']['guest']['exit'] == 3,
            'original execution mismatch')
    invocation = prior['guest_invocation']
    require(invocation == 'ec4bba4c-ce01-44ff-9b52-700d5446c588', 'original invocation mismatch')
    require(git('rev-parse', 'HEAD~2' if setup_only else 'HEAD^') == prior['carrier'], 'recovery ancestry mismatch')
    DOC.update(original=dict(artifact=13253001262, zip_sha256=sha(raw), receipt=prior),
               scope='Original guest attempts and diagnostics only; no build or test rerun',
               workload_repeated=False, guest_invocation=invocation, targets={}, recovered_attempts=[])
    save()
    previous = None
    if setup_only:
        recovered = (TMP / 'reports.zip').read_bytes()
        require(sha(recovered) == '1f6929cfa8c8fecda9626a57bff3a139caba9ce73dd627e9a20ae00d8ba5d885', 'recovered reports ZIP mismatch')
        with zipfile.ZipFile(io.BytesIO(recovered)) as archive:
            require(archive.namelist() == ['container-native-result.json'], 'recovered reports members mismatch')
            previous = json.loads(archive.read('container-native-result.json'))
        require(previous['carrier'] == '0d0980f0a59a26b385424f8810356cbe84315c04'
                and previous['original']['receipt'] == prior and previous['files_complete'], 'recovered reports identity mismatch')
        DOC.update(scope='Two original boot-test setup logs and output inventories; no workload rerun',
                   prior_reports=dict(artifact=13253356296, zip_sha256=sha(recovered)))
    known_tools = set('bash sh busybox tar mount mkfs.erofs stat awk sha256sum cut chmod ln cat qemu-system-x86_64'.split())
    known_tools.update(NAMES)

    def project(data):
        patterns = {
            'missing_file': r'(?i)no such file or directory|command not found|not found in .?PATH',
            'permission': r'(?i)permission denied|operation not permitted',
            'exec_format': r'(?i)exec format error|cannot execute binary file',
            'shared_library': r'(?i)error while loading shared libraries|GLIBC_[0-9.]+.*not found',
            'kvm': r'(?i)could not access KVM|failed to initialize kvm|KVM.*not supported',
            'kernel_panic': r'Kernel panic|not syncing|VFS: Unable to mount root',
            'sharding': r'(?i)sharding.*(?:requested|support)|TEST_SHARD_STATUS_FILE',
            'erofs': r'(?i)mkfs[.]erofs.*(?:error|failed|invalid|usage)|erofs.*(?:not found|unsupported)',
            'timeout': r'(?i)timed out|^TIMEOUT$|deadline exceeded',
            'disk': r'(?i)no space left on device',
        }
        result = dict(diagnostics=[], host_phases=[], fixed_messages=[], guest_phases=[], guest_observations=[])
        fixed = {'Guest did not report a completed test', 'Passing guest omitted test XML',
                 'The guest CPU does not expose VMX or SVM.', 'ARM64 guest KVM device unavailable',
                 'Guest core scheduling unavailable', 'Guest CPUs are not SMT siblings'}
        for line in data.decode(errors='replace').splitlines():
            if re.search(r'(?i)authorization|x-buildbuddy-api-key|remote_header|bes_header|github_pat_|gh[pousr]_', line):
                continue
            classes = sorted(k for k, pattern in patterns.items() if re.search(pattern, line))
            if classes and len(result['diagnostics']) < 32:
                result['diagnostics'].append(dict(classes=classes, line_sha256=sha(line.encode()),
                    tools=sorted(tool for tool in known_tools if re.search(
                        r'(?<![A-Za-z0-9_.+-])' + re.escape(tool) + r'(?![A-Za-z0-9_.+-])', line))))
            if line in fixed:
                result['fixed_messages'].append(line)
            match = re.fullmatch(r'host_uptime_seconds=([0-9.]+) phase=([a-z_]{1,40}) status=(-|[0-9]{1,3}) outer_test_timeout_seconds=(unset|[0-9]{1,5})', line)
            if match:
                result['host_phases'].append(dict(uptime=match[1], phase=match[2], status=match[3]))
            match = re.fullmatch(r'guest_uptime_seconds=([0-9.]+) phase=(init_start|payload_mount_start|payload_mount_return|output_copy_start|poweroff_start)', line)
            if match:
                result['guest_phases'].append(dict(uptime=match[1], phase=match[2]))
            for pattern in (
                r'Guest KVM module: kvm_(?:intel|amd)',
                r'Guest page size: [0-9]+',
                r'Guest work filesystem: block_size=[0-9]+ blocks=[0-9]+',
                r'Payload executable: uid=[0-9]+ gid=[0-9]+ mode=[0-7]+',
            ):
                if re.fullmatch(pattern, line):
                    result['guest_observations'].append(line)
        return result

    require(project(b'Authorization: Bearer private')['diagnostics'] == [], 'header projection control')
    sample = project(b'/private/tool/mkfs.erofs: error while loading shared libraries: private.so')
    require(sample['diagnostics'][0]['classes'] == ['erofs', 'shared_library']
            and 'private' not in json.dumps(sample), 'diagnostic projection control')
    require(not project(b'host_uptime_seconds=1.0 phase=launcher_start status=- outer_test_timeout_seconds=900')['diagnostics'], 'timeout field is not a timeout')
    DOC['projection_control'] = 'PASS'
    seen = set()
    labels = ['//' + PKG + ':' + mode + '_' + name for mode in ('v1', 'v2') for name in (('boot_test',) if setup_only else NAMES)]

    def target(label):
        response = rpc('GetTarget', dict(invocationId=invocation, targetLabel=label))
        row = DOC['targets'][label] = dict(metadata_sha256=sha(json.dumps(response, sort_keys=True).encode()), attempts=0)
        save()
        groups = response.get('targetGroups', [])
        require(groups and all(not g.get('nextPageToken') for g in groups), 'partial target metadata')
        events_by_id = {}
        for group in groups:
            for value in group.get('targets', []):
                require(value['metadata']['label'] == label, 'wrong returned owner')
                for event in value.get('testResultEvents', []):
                    ident = event['id']['testResult']
                    require(ident['label'] == label, 'wrong attempt owner')
                    configuration = ident['configuration']['id']
                    require(re.fullmatch('[a-f0-9]{64}', configuration), 'invalid configuration')
                    key = (label, configuration, int(ident.get('run', 0)), int(ident.get('shard', 0)), int(ident.get('attempt', 0)))
                    if key in events_by_id:
                        require(events_by_id[key] == event, 'conflicting repeated event')
                    events_by_id[key] = event
        mode, name = label.split(':')[1].split('_', 1)
        require(len(events_by_id) == SHARDS[name], 'missing or extra original attempts')
        require({key[3] for key in events_by_id} == set(range(1, SHARDS[name] + 1))
                and len({key[1] for key in events_by_id}) == 1, 'incomplete shards or mixed configurations')
        for key, event in sorted(events_by_id.items()):
            _, configuration, run, shard, attempt = key
            require(run == attempt == 1 and 1 <= shard <= SHARDS[name] and key not in seen, 'unexpected attempt identity')
            seen.add(key)
            result = event['testResult']
            previous_attempt = None
            if setup_only:
                previous_attempt, = (a for a in previous['recovered_attempts'] if a['guest_owner'] == label)
                require(previous_attempt['configuration'] == configuration and previous_attempt['status'] == result['status'] == 'FAILED'
                        and previous_attempt['run'] == run and previous_attempt['bep_shard'] == shard
                        and previous_attempt['attempt'] == attempt, 'prior setup attempt mismatch')
            item = dict(owner='//runsc/container:' + name, guest_owner=label, configuration=configuration,
                mode=mode, run=run, bep_shard=shard, shard_index=shard - 1, attempt=attempt,
                status=result['status'], local_cache=bool(result.get('cachedLocally')),
                remote_cache=bool(result.get('executionInfo', {}).get('cachedRemotely')),
                strategy=result.get('executionInfo', {}).get('strategy'), timing={}, files=[])
            for field, pattern in (
                ('testAttemptStartMillis', r'[0-9]{1,16}'), ('testAttemptDurationMillis', r'[0-9]{1,16}'),
                ('testAttemptStart', r'[0-9T:.+-]{10,40}Z?'), ('testAttemptDuration', r'[0-9.]{1,24}s'),
            ):
                value = result.get(field)
                if isinstance(value, (str, int)) and re.fullmatch(pattern, str(value)):
                    item['timing'][field] = value
            DOC['recovered_attempts'].append(item)
            row['attempts'] += 1
            save()
            for entry in result.get('testActionOutput', []):
                allowed = ('test.log', 'test.xml', 'test.outputs__outputs.zip', 'test.outputs/outputs.zip') if setup_only else ('test.log', 'test.xml')
                if entry.get('name') not in allowed:
                    continue
                uri = entry.get('uri', '')
                match = re.fullmatch(r'bytestream://remote\.buildbuddy\.io/(?:[^?#]*/)?blobs/([a-f0-9]{64})/(\d+)', uri)
                file = dict(name=entry['name'], disposition='unavailable')
                item['files'].append(file)
                if not match:
                    save()
                    continue
                file.update(sha256=match[1], bytes=int(match[2]))
                save()
                if not 0 < int(match[2]) < 2 * 1024**2:
                    file['disposition'] = 'outside_size_bound'
                    save()
                    continue
                data = blob(dict(hash=match[1], sizeBytes=match[2]), invocation)
                if setup_only and entry['name'] in ('test.log', 'test.xml'):
                    earlier, = (f for f in previous_attempt['files'] if f['name'] == entry['name'])
                    require(earlier['sha256'] == sha(data) and earlier['bytes'] == len(data), 'original setup report bytes changed')
                if entry['name'] in ('test.outputs__outputs.zip', 'test.outputs/outputs.zip'):
                    # List only declared initializer/adapter outputs; never copy
                    # arbitrary guest paths, logs, environment or checkpoint data.
                    names = ('console.log', 'guest/exit_status', 'guest/kernel.txt', 'guest/kernel.config',
                             'guest/root-image.txt', 'guest/page-size.txt', 'guest/kvm.txt',
                             'guest/limits-before.txt', 'guest/limits.txt', 'guest/test.xml',
                             'guest/outputs/proof', 'guest/shard_status')
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        require(len(archive.infolist()) < 1024 and sum(i.file_size for i in archive.infolist()) < 8 * 1024**2, 'setup output ZIP bound')
                        require(len(set(archive.namelist())) == len(archive.namelist()), 'duplicate setup output')
                        file['known_members'] = []
                        for member in names:
                            if member not in archive.namelist():
                                continue
                            contents = archive.read(member)
                            item_info = dict(name=member, bytes=len(contents), sha256=sha(contents))
                            if member == 'guest/exit_status':
                                require(re.fullmatch(rb'[0-9]{1,3}\n', contents), 'invalid retained guest status')
                                item_info['exit'] = int(contents)
                            file['known_members'].append(item_info)
                        file['inventory_observation'] = 'KNOWN_MEMBERS_RECORDED' if file['known_members'] else 'ZIP_INSPECTED_NO_KNOWN_MEMBERS'
                elif entry['name'] == 'test.xml':
                    cases = list(ET.fromstring(data).iter('testcase'))
                    file.update(cases=len(cases), failed=sum(c.find('failure') is not None or c.find('error') is not None for c in cases),
                        substantive_cases=sum(c.get('name', '').startswith('Test') for c in cases))
                    original = [a for a in prior['attempts'] if a['owner'] == item['owner'] and a['mode'] == mode and a['shard'] == shard]
                    if original:
                        old, = original
                        require(old['xml']['sha256'] == sha(data) and old['status'] == item['status'], 'retained original attempt differs')
                        file['matches_original_receipt'] = True
                else:
                    file.update(project(data))
                file['disposition'] = 'recovered'
                save()
        require(row['attempts'] == SHARDS[name], 'incomplete attempt count')

    for label in labels:
        phase(label, lambda label=label: target(label))
    DOC['expected_attempts'] = 2 if setup_only else 26
    DOC['attempt_inventory_complete'] = len(seen) == DOC['expected_attempts'] and all(p['status'] == 'PASS' for p in DOC['phases'].values())
    DOC['files_complete'] = DOC['attempt_inventory_complete'] and all(
        {'test.log', 'test.xml'} <= {f['name'] for f in a['files'] if f['disposition'] == 'recovered'}
        for a in DOC['recovered_attempts'])
    if setup_only:
        DOC['output_inventories_complete'] = DOC['attempt_inventory_complete'] and all(
            any('known_members' in f and f['disposition'] == 'recovered' for f in a['files'])
            for a in DOC['recovered_attempts'])
    DOC['status'] = 'ORIGINAL_FAIL_REPORTS_RECOVERED' if DOC['files_complete'] else 'ORIGINAL_FAIL_RECOVERY_INCOMPLETE'
    save()


def recover_coretag():
    """Read the two already-completed topology arms; never boot another VM."""
    from cas_inputs import blob, rpc
    import coretag_original
    import unit_v1

    raw = (TMP / 'coretag.zip').read_bytes()
    require(sha(raw) == '6b7eb6df084a876265207777ae33dc5b1ad49a73053b3b1fbb99eb4017794128', 'coretag ZIP mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.namelist() == ['verdict.json'], 'coretag members mismatch')
        prior = json.loads(archive.read('verdict.json'))
    require(prior['carrier'] == '64fea59014b0104240a5e100095b60e51aa90bae' and prior['status'] == 'FAIL'
            and prior['product'] == '96fadde722e14d482a62276b80237398f8b62fbf'
            and prior['guest_source'] == GUEST and prior['coordinator']['machine'] == 'x86_64'
            and prior['coordinator']['vendor'] == 'AuthenticAMD', 'coretag original identity mismatch')
    rows = [prior['phases'][name] for name in ('original_cpu', 'topoext_cpu')]
    require([row['native_exit'] for row in rows] == [3, 0], 'coretag original exits mismatch')
    require(rows[0]['suite_executed'][0]['execution_info']['hostname'] == rows[1]['suite_executed'][0]['execution_info']['hostname'], 'coretag workers differ')
    DOC['coretag'] = dict(original=dict(artifact=13252722859, zip_sha256=sha(raw), receipt=prior),
                         workload_repeated=False, whole_unit_suite_qualified=False, arms={})
    save()
    for name, old in zip(('original_cpu', 'topoext_cpu'), rows):
        original, = old['suite_executed']
        invocation, label = old['invocation'], original['owner']['label']
        require(invocation == {'original_cpu': '0b7450ef-3e4d-4a99-816a-842ff90c63c0',
                               'topoext_cpu': '85461e86-3bf6-4263-8b22-967a048f5a24'}[name], 'coretag invocation mismatch')
        require(not original['cached_locally'] and not original['execution_info'].get('cachedRemotely')
                and original['execution_info']['strategy'] == 'local', 'coretag original placement mismatch')
        response = rpc('GetTarget', dict(invocationId=invocation, targetLabel=label))
        arm = DOC['coretag']['arms'][name] = dict(invocation=invocation, owner=label,
            metadata_sha256=sha(json.dumps(response, sort_keys=True).encode()), files=[])
        save()
        observed = {}
        for group in response.get('targetGroups', []):
            require(not group.get('nextPageToken'), 'partial coretag target')
            for value in group.get('targets', []):
                require(value['metadata']['label'] == label, 'wrong coretag owner')
                for event in value.get('testResultEvents', []):
                    observed[sha(json.dumps(event, sort_keys=True).encode())] = event
        event, = observed.values()
        ident, result = event['id']['testResult'], event['testResult']
        require(ident['label'] == label and ident['configuration']['id'] == original['owner']['configuration']
                and all(int(ident.get(key, 0)) == original[key] == 1 for key in ('run', 'shard', 'attempt')),
                'coretag attempt identity mismatch')
        require(result['status'] == original['status'] and not result.get('cachedLocally')
                and not result.get('executionInfo', {}).get('cachedRemotely')
                and result['executionInfo']['strategy'] == 'local', 'coretag attempt result mismatch')
        files = {}
        for entry in result.get('testActionOutput', []):
            kind = entry.get('name')
            if kind not in {'test.log', 'test.xml', 'test.outputs__outputs.zip', 'test.outputs/outputs.zip'}:
                continue
            match = re.fullmatch(r'bytestream://remote\.buildbuddy\.io/(?:[^?#]*/)?blobs/([a-f0-9]{64})/(\d+)', entry.get('uri', ''))
            require(match and 0 < int(match[2]) < 4 * 1024**2, 'coretag file location or size')
            kind = 'outputs.zip' if kind in {'test.outputs__outputs.zip', 'test.outputs/outputs.zip'} else kind
            require(kind not in files, 'duplicate coretag file')
            arm['files'].append(dict(name=kind, sha256=match[1], bytes=int(match[2])))
            save()
            files[kind] = blob(dict(hash=match[1], sizeBytes=match[2]), invocation)
        require({'test.log', 'test.xml'} <= set(files), 'missing coretag reports')
        arm['topology'] = coretag_original.topology(files['test.log'])
        require(arm['topology'] == old['topology'], 'coretag original log changed')
        cases = list(ET.fromstring(files['test.xml']).iter('testcase'))
        arm['xml'] = dict(cases=len(cases), test_enable_count=sum(c.get('name') == 'TestEnable' for c in cases))
        save()
        if name == 'original_cpu':
            require(arm['topology']['topology_rejected'] and arm['xml']['test_enable_count'] == 0, 'original topology boundary changed')
            arm['observation'] = 'TOPOLOGY_REJECTED'
            continue
        require(len(cases) == 1 and cases[0].get('name') == 'TestEnable' and not list(cases[0])
                and cases[0].get('status') != 'notrun' and cases[0].get('result') != 'suppressed', 'TestEnable not executed successfully')
        topology = arm['topology']
        require(topology['config_sched_core'] and topology['guest_completed'] and topology['topoext'] == [True]
                and topology['online'] == ['0-1'] and topology['vendors'] == ['AuthenticAMD']
                and topology['siblings'] == [dict(cpu=cpu, mask='0-1') for cpu in (0, 1)], 'native SMT proof differs')
        require('outputs.zip' in files, 'missing coretag v1 proof')
        path = TMP / 'coretag-outputs.zip'
        path.write_bytes(files['outputs.zip'])
        arm['guest'] = unit_v1.modes(path, 'guest/outputs/')
        require(arm['guest'].get('mode') == 'false' and arm['guest'].get('machine') == 'x86_64'
                and arm['guest'].get('elf_sha256') == coretag_original.ELF, 'coretag v1 or original ELF proof missing')
        arm['observation'] = 'NATIVE_KVM_SMT_PASS'
        DOC['coretag']['native_kvm_test_enable_passed'] = True
        save()


def recover_build():
    """Read the exact retained stderr, without invoking the workload."""
    from cas_inputs import blob

    original = (TMP / 'original.zip').read_bytes()
    require(sha(original) == '3b973e531ad21fcb9051862551b435d1556b93eeecdef9e2c8c1bfcb36f2ff39', 'original ZIP mismatch')
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        require(archive.namelist() == ['container-native-result.json'], 'original members mismatch')
        prior = json.loads(archive.read('container-native-result.json'))
    require(prior['carrier'] == '9b79f6af77eb770b205bb12ff8588c8f75d8c1d1' and prior['status'] == 'FAIL'
            and prior['source'] == SOURCE and prior['product'] == PRODUCT and not prior['attempts'], 'original source mismatch')
    require(prior['commands']['native-build']['exit'] == 1, 'original build exit mismatch')
    evidence = prior['native_build_evidence']
    require(evidence['invocation'] == '92e35f5e-55d5-4d74-a922-b867bae9da00', 'invocation mismatch')
    action, = evidence['failed_actions']
    require(action['owner'] == '//tools/nogo:stdlib' and action['exit'] == 1
            and action['configuration'] == '109b925577db5c4e66db055b0522b402102950532da954a8620c8c4c4d4329d3'
            and action['mnemonic'] == 'GoStandardLibraryAnalysis'
            and action['primary_output'] == 'bazel-out/k8-fastbuild/bin/tools/nogo/stdlib.facts', 'action mismatch')
    stderr, = action['files']
    require(stderr['name'] == 'stderr' and stderr['bytes'] == 96
            and stderr['sha256'] == '9ae35684375799d6f9248bc76bc7095ef5123b3aad2d3170ff1c1c0e6bf6fce2', 'stderr mismatch')
    DOC.update(original=dict(artifact=13248899435, zip_sha256=sha(original), receipt=prior),
               scope='One original failed action stderr; no build or tests repeated', workload_repeated=False)
    save()
    data = blob(dict(hash=stderr['sha256'], sizeBytes=stderr['bytes']), evidence['invocation'])
    require(len(data) == 96 and sha(data) == stderr['sha256'], 'recovered bytes differ')
    # Selection is already the exact failed action's small stderr, so allow
    # lowercase source-owned errors through the existing sanitization too.
    lines = diagnostic_lines(b'Error: ' + data)
    require(lines and all(len(line) <= 480 for line in lines), 'diagnostic not safely projectable')
    DOC.update(status='ORIGINAL_FAIL_DIAGNOSTIC_RECOVERED', recovered_stderr=dict(
        sha256=sha(data), bytes=len(data), diagnostics=lines))
    save()


def main():
    original = (TMP / 'original.zip').read_bytes()
    require(sha(original) == 'd138fc483e8a18f06b40438422edf3aedaa622f547390108e835c72aa2a38679', 'original ZIP mismatch')
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        require(archive.namelist() == ['container-native-result.json'], 'original members mismatch')
        prior = json.loads(archive.read('container-native-result.json'))
    require(prior['carrier'] == 'c063181c68d6228cb4d34e7d897c4fa8f485d2c0' and prior['status'] == 'FAIL'
            and prior['source'] == SOURCE and prior['product'] == PRODUCT
            and prior['commands']['native-build']['exit'] == 1 and not prior['attempts'], 'unexpected original execution')
    DOC['original'] = dict(artifact=13248882573, zip_sha256=sha(original), receipt=prior,
                          test_workload_repeated=False, build_failure_diagnostic='not retained')
    recovery = (TMP / 'diagnostic.zip').read_bytes()
    require(sha(recovery) == '87c80db9c15b59d55bed91b91db1e4231e1062a7c3c17fc8136f2d8a1db5c414', 'diagnostic ZIP mismatch')
    with zipfile.ZipFile(io.BytesIO(recovery)) as archive:
        require(archive.namelist() == ['container-native-result.json'], 'diagnostic members mismatch')
        diagnostic = json.loads(archive.read('container-native-result.json'))
    require(diagnostic['status'] == 'ORIGINAL_FAIL_DIAGNOSTIC_RECOVERED' and diagnostic['source'] == SOURCE and diagnostic['product'] == PRODUCT and diagnostic['carrier'] == '27ce26ffee21c40458a9f106d2ac10e3cb70ef82', 'diagnostic source mismatch')
    require(diagnostic['recovered_stderr']['sha256'] == '9ae35684375799d6f9248bc76bc7095ef5123b3aad2d3170ff1c1c0e6bf6fce2', 'diagnostic stderr mismatch')
    DOC['original_diagnostic'] = dict(artifact=13251075654, zip_sha256=sha(recovery), receipt=diagnostic)
    sample = diagnostic_lines(b'ERROR: public/BUILD:1:2: missing input\nAuthorization: Bearer private\ncompilepkg: missing dependency https://private@example.test/x?token=private\nERROR: grpcs://private@example.test/x?token=private')
    require(sample == ['ERROR: public/BUILD:1:2: missing input', 'compilepkg: missing dependency [URL omitted]', 'ERROR: [URL omitted]'], 'diagnostic projection control')
    DOC['diagnostic_projection_control'] = 'PASS'
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
            'build --bes_backend=grpcs://remote.buildbuddy.io\n'
            'build --bes_results_url=https://app.buildbuddy.io/invocation/\n'
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
    elif set(DOC['phases']) == {'native_make_and_capsules', 'stdlib_producer_evidence', 'guest_v1_v2'} and all(r['status'] == 'PASS' for r in DOC['phases'].values()):
        DOC['status'] = 'PASS'
    save()


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--recover-guest-setup']:
            recover_guests(setup_only=True)
        elif sys.argv[1:] == ['--recover-guests']:
            recover_guests()
            DOC['container_recovery_status'] = DOC['status']
            phase('coretag_reports', recover_coretag)
            if DOC['phases']['coretag_reports']['status'] != 'PASS':
                DOC['status'] = 'ORIGINAL_FAIL_RECOVERY_INCOMPLETE'
            save()
        elif sys.argv[1:] == ['--recover-build']:
            recover_build()
        else:
            main()
    except Exception as exc:
        DOC['error_class'] = type(exc).__name__
        save()
    sys.exit(0 if DOC['status'] in ('PASS', 'ORIGINAL_FAIL_DIAGNOSTIC_RECOVERED', 'ORIGINAL_FAIL_REPORTS_RECOVERED') else 1)
