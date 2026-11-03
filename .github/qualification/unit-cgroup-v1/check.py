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
import urllib.error
import urllib.parse
import urllib.request
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
    row = dict(started_utc=now(), exit=None, kind='test' if name.startswith('guest') else 'build')
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
    require(git('rev-parse', 'HEAD^') == 'a7d6b51bd83d8a9aedfcd58720aeedddad7308ed'
            and git('rev-parse', 'HEAD~7') == GUEST, 'guest source mismatch')
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
    args = ['bazel', '--bazelrc=' + str(rc), 'test', '--config=rbe', '--config=rbe-local-tests',
            '--//tools/bazeldefs:local_test_architecture=amd64', '--jobs=80', '--local_test_jobs=2',
            '--nocache_test_results', '--flaky_test_attempts=1', '--runs_per_test=1', '--keep_going',
            '--zip_undeclared_test_outputs', '--nobuild_event_json_file_path_conversion', '--test_timeout=900', '--test_output=errors']
    rows = []
    DOC['guest_invocations'] = []
    boot = [label for label in labels if label.endswith(':v1_boot_test') or label.endswith(':v2_boot_test')]
    for command, selection in (('guest-boot', boot), ('guest', [label for label in labels if label not in boot])):
        bep = TMP / (command + '.bep')
        code = execute(command, args + ['--build_event_json_file=' + str(bep), *selection], timeout=1800)
        current = events(bep)
        invocation = next(e['started']['uuid'] for e in current if 'started' in e)
        DOC['guest_invocations'].append(dict(command=command, invocation=invocation, bep_sha256=sha(bep.read_bytes())))
        results = [e for e in current if 'testResult' in e]
        rows.extend((invocation, event) for event in results)
        save()
        if command == 'guest-boot':
            ready = code == 0 and len(results) == 2 and {e['id']['testResult']['label'] for e in results} == set(boot)
            for event in results:
                result = event['testResult']
                ready = ready and result['status'] == 'PASSED'
                try:
                    uri, = (urlsplit(f['uri']) for f in result['testActionOutput'] if f.get('name') == 'test.xml')
                    require(uri.scheme == 'file' and uri.netloc in ('', 'localhost'), 'nonlocal boot XML')
                    path = Path(unquote(uri.path))
                    require(path.stat().st_size < 16 * 1024**2, 'boot XML bound')
                    counts = xml_cases(path)
                    passed_names = {c.get('name') for c in ET.parse(path).iter('testcase')
                        if not any(c.find(k) is not None for k in ('failure', 'error', 'skipped'))
                        and c.get('status') != 'notrun' and c.get('result') != 'suppressed'}
                    ready = ready and counts['failed'] == 0 and {'TestMinimalBoot/systrap', 'TestMinimalBoot/kvm'} <= passed_names
                except Exception:
                    ready = False
            DOC['boot_gate_passed'] = ready
            save()
            if not ready:
                break
    DOC['execution_finished_utc'] = now()
    save()
    seen = set()
    configurations = {}
    leaves_by_owner = {}
    passed = {mode: set() for mode in ('v1', 'v2')}
    for invocation, event in rows:
        ident, result = event['id']['testResult'], event['testResult']
        label = ident['label']
        require(label in labels, 'unexpected guest owner')
        mode, name = label.split(':')[1].split('_', 1)
        bep_shard = int(ident.get('shard', 0))
        shard = bep_shard - 1
        configuration = ident['configuration']['id']
        require(re.fullmatch('[a-f0-9]{64}', configuration)
                and int(ident.get('run', 0)) == int(ident.get('attempt', 0)) == 1, 'invalid attempt identity')
        require(configurations.setdefault(label, configuration) == configuration, 'mixed guest configurations')
        key = (mode, name, shard)
        require(key not in seen and shard in range(SHARDS[name]), 'duplicate or wrong shard')
        seen.add(key)
        row = dict(owner='//runsc/container:' + name, invocation=invocation, mode=mode, shard=shard, bep_shard=bep_shard, configuration=configuration,
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
            log = local_file('test.log')
            require(log.stat().st_size < 16 * 1024**2, 'guest log size bound')
            content = log.read_bytes()
            row['log'] = dict(sha256=sha(content), bytes=len(content), setup_failures=[
                dict(stage=m[1].decode(), exit=int(m[2])) for m in re.finditer(
                    rb'^NATIVE_CONTAINER_SETUP stage=(capsule_identity|worker_contract|hierarchy|extract_capsule|extract_erofs|executable_identity|erofs_files|erofs_cli|proof|exec) exit=([0-9]{1,3})\r?$', content, re.M)],
                erofs_cli_control=bool(re.search(rb'^NATIVE_CONTAINER_EROFS rejected_version_exit=1 help_exit=0\r?$', content, re.M)))
            save()
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
    require(all(r['log']['erofs_cli_control'] and not r['log']['setup_failures'] for r in DOC['attempts']), 'EROFS preflight did not pass')
    require(code == 0 and all(r['status'] == 'PASSED' and r['xml']['failed'] == 0 and r['xml']['passed'] > 0 for r in DOC['attempts']), 'guest functional failure')
    require(all({'TestMinimalBoot/systrap', 'TestMinimalBoot/kvm', 'TestMountEROFS', 'TestMountEROFSConfig/image1'} <= names for names in passed.values()), 'required platform or EROFS case did not pass')


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


def recover_capsules(prior):
    """Materialize the six original outputs; never substitute a new build."""
    from cas_inputs import KEY, OPENER

    invocation = prior['native_build_evidence']['invocation']
    require(invocation == '134254d6-7c51-4854-9a1e-fdfb2ce9ddc6', 'original native invocation mismatch')
    require(set(prior['payloads']) == set(NAMES), 'original capsule set mismatch')
    DOC['capsule_recovery'] = {}
    for name, original in prior['payloads'].items():
        expected, size = original['sha256'], original['bytes']
        require(re.fullmatch('[0-9a-f]{64}', expected) and 0 < size < 300 * 1024**2, 'capsule bound')
        row = DOC['capsule_recovery'][name] = dict(sha256=expected, bytes=size, status='INCOMPLETE')
        save()
        uri = 'bytestream://remote.buildbuddy.io/blobs/' + expected + '/' + str(size)
        url = 'https://app.buildbuddy.io/file/download?' + urllib.parse.urlencode(dict(bytestream_url=uri, invocation_id=invocation))
        request = urllib.request.Request(url, headers={'x-buildbuddy-api-key': KEY})
        destination = TMP / (name + '.tar')
        digest, total = hashlib.sha256(), 0
        # The established reader's no-redirect opener preserves the credential
        # boundary. Stream these exact larger outputs with their own byte cap.
        try:
            with OPENER.open(request, timeout=120) as response, destination.open('xb') as output:
                while data := response.read(1024**2):
                    total += len(data)
                    require(total <= size, 'capsule exceeds original length')
                    digest.update(data)
                    output.write(data)
        except Exception as exc:
            row.update(error_class=type(exc).__name__, received_bytes=total)
            if isinstance(exc, urllib.error.HTTPError):
                row['http_status'] = exc.code
            save()
            raise
        require(total == size and digest.hexdigest() == expected, 'original capsule bytes differ')
        with tarfile.open(destination) as archive:
            binary = archive.extractfile(original['binary']).read()
            require(sha(binary) == original['elf_sha256'] and len(binary) == original['elf_bytes']
                    and binary[:6] == b'\x7fELF\x02\x01' and int.from_bytes(binary[18:20], 'little') == 62, 'original capsule ELF differs')
            if original['runsc_sha256']:
                runsc = archive.extractfile(original['runfiles'] + '/_main/release/runsc').read()
                require(sha(runsc) == original['runsc_sha256'], 'original runsc differs')
        DOC['payloads'][name] = original
        row['status'] = 'RECOVERED'
        save()


def main():
    original = (TMP / 'original.zip').read_bytes()
    require(sha(original) == '065c02f43387d0cdaa4c5417d2f89feec88202ebb9ca44c99d6225a1d4ce46e5', 'original ZIP mismatch')
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        require(archive.namelist() == ['container-native-result.json'], 'original members mismatch')
        prior = json.loads(archive.read('container-native-result.json'))
    require(prior['carrier'] == 'c90e30503332735ddb87d992588101661a7e2d1c' and prior['status'] == 'FAIL'
            and prior['source'] == SOURCE and prior['product'] == PRODUCT
            and prior['commands']['native-build']['exit'] == 0 and prior['commands']['guest']['exit'] == 3
            and prior['phases']['native_make_and_capsules']['status'] == 'PASS', 'unexpected original execution')
    DOC.update(original=dict(artifact=13253001262, zip_sha256=sha(original), receipt=prior),
               scope='Corrected guest adapter using six exact original native capsules; no product build',
               product_build_repeated=False, modes=prior['modes'])
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
    phase('recover_capsules', lambda: recover_capsules(prior))
    if DOC['phases']['recover_capsules']['status'] != 'PASS':
        return
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
        phase('guest_v1_v2', lambda: guest(rc))
    if any(r['exit'] not in (0, None, 124) and r.get('disposition') != 'VALIDATED_ANALYSIS_ONLY' for r in DOC['commands'].values()):
        DOC['status'] = 'FAIL'
    elif set(DOC['phases']) == {'recover_capsules', 'guest_v1_v2'} and all(r['status'] == 'PASS' for r in DOC['phases'].values()):
        DOC['status'] = 'PASS'
    save()


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--recover-build']:
            recover_build()
        else:
            main()
    except Exception as exc:
        DOC['error_class'] = type(exc).__name__
        save()
    sys.exit(0 if DOC['status'] in ('PASS', 'ORIGINAL_FAIL_DIAGNOSTIC_RECOVERED') else 1)
