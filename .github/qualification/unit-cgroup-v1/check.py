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
PRIOR_SOURCE = '7e29426b047a41303652cfa9f0ae91e933302f5e'
LOOKUP = 'b2bd6284f19ec076f76aa0ce3d00494e69f75b0b'
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
    require(git('rev-parse', SOURCE + '^') == PRIOR_SOURCE
            and git('rev-parse', PRIOR_SOURCE + '^') == PRODUCT, 'wrong lookup/compiler integration parents')
    changed = git('diff-tree', '--no-commit-id', '--name-only', '-r', SOURCE).splitlines()
    require(len(changed) == 8 and all(p.startswith(('pkg/test/testutil/', 'runsc/container/')) for p in changed), 'unexpected lookup scope')
    require(not git('diff', '--name-only', LOOKUP, SOURCE, '--', *changed), 'qualified lookup source differs')
    DOC['lookup_fix'] = dict(source=LOOKUP, integrated_files=changed)
    DOC['bazel_version'] = subprocess.check_output(['bazel', '--version'], text=True).strip()
    require(DOC['bazel_version'] == 'bazel ' + Path('.bazelversion').read_text().strip(), 'wrong Bazel')
    bazel = ['bazel', '--bazelrc=' + str(rc)]
    prior = DOC['original']['receipt']['original']['receipt']
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
            old = DOC['original']['receipt']['payloads'][name]
            DOC['payloads'][name]['matches_original'] = {key: DOC['payloads'][name][key] == old[key]
                for key in ('sha256', 'elf_sha256', 'runsc_sha256')}
            save()
    finally:
        build.write_text(original)
    require(not git('diff', '--name-only', 'HEAD'), 'product source changed')


def guest(rc):
    git('switch', '--quiet', '--detach', CARRIER)
    require(git('rev-parse', 'HEAD^') == '312a88ac731277a0e435ec4044432605342a1f51'
            and git('rev-parse', 'HEAD~10') == GUEST, 'guest source mismatch')
    package = Path(PKG)
    for name, row in DOC['payloads'].items():
        shutil.copyfile(TMP / (name + '.tar'), package / (name + '.tar'))
        require(sha((package / (name + '.tar')).read_bytes()) == row['sha256'], 'capsule changed')
    (package / 'inputs.json').write_text(json.dumps(DOC['payloads']))
    # Shell-readable paths/hashes are source-derived and restricted before writing.
    for name, row in DOC['payloads'].items():
        require(all(re.fullmatch(r'[A-Za-z0-9_./+-]+', row[key]) for key in ('binary', 'runfiles')), 'invalid archive path')
        (package / (name + '.inputs')).write_text('\n'.join(row[key] or '-' for key in ('sha256', 'binary', 'runfiles', 'elf_sha256', 'runsc_sha256')) + '\n')
    selections = {
        '//' + PKG + ':v1_annotation': ('v1', 'annotation', 0, 1),
        '//' + PKG + ':v2_annotation': ('v2', 'annotation', 0, 1),
        '//' + PKG + ':v1_missing_shard_4': ('v1', 'missing-shard-4', 4, 8),
    }
    labels = sorted(selections)
    args = ['bazel', '--bazelrc=' + str(rc), 'test', '--config=rbe', '--config=rbe-local-tests',
            '--//tools/bazeldefs:local_test_architecture=amd64', '--jobs=80', '--local_test_jobs=2',
            '--nocache_test_results', '--flaky_test_attempts=1', '--runs_per_test=1', '--keep_going',
            '--zip_undeclared_test_outputs', '--nobuild_event_json_file_path_conversion', '--test_timeout=1200', '--test_output=errors']
    bep = TMP / 'guest-remaining.bep'
    code = execute('guest-remaining', args + ['--build_event_json_file=' + str(bep), *labels], timeout=2400)
    current = events(bep)
    invocation = next(e['started']['uuid'] for e in current if 'started' in e)
    DOC['guest_invocations'] = [dict(command='guest-remaining', invocation=invocation, bep_sha256=sha(bep.read_bytes()))]
    rows = [(invocation, event) for event in current if 'testResult' in event]
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
        mode, selection, shard, total_shards = selections[label]
        name = 'container_test'
        bep_shard = int(ident.get('shard', 0))
        require(bep_shard == 1, 'unexpected outer shard')
        configuration = ident['configuration']['id']
        require(re.fullmatch('[a-f0-9]{64}', configuration)
                and int(ident.get('run', 0)) == int(ident.get('attempt', 0)) == 1, 'invalid attempt identity')
        require(configurations.setdefault(label, configuration) == configuration, 'mixed guest configurations')
        key = (mode, selection)
        require(key not in seen and shard in range(SHARDS[name]), 'duplicate or wrong shard')
        seen.add(key)
        row = dict(owner='//runsc/container:' + name, guest_owner=label, invocation=invocation, mode=mode, selection=selection, shard=shard, bep_shard=bep_shard, configuration=configuration,
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
                    rb'^NATIVE_CONTAINER_SETUP stage=(capsule_identity|worker_contract|hierarchy|extract_capsule|extract_erofs|executable_identity|erofs_files|erofs_cli|strings|selection|proof|exec) exit=([0-9]{1,3})\r?$', content, re.M)],
                erofs_cli_control=bool(re.search(rb'^NATIVE_CONTAINER_EROFS rejected_version_exit=1 help_exit=0\r?$', content, re.M)),
                strings_control=bool(re.search(rb'^NATIVE_CONTAINER_STRINGS original_absent=1 applet=PASS alias=PASS\r?$', content, re.M)))
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
            require(len(proof) == 16 and proof[5] == 'CGROUPV2=' + ('false' if mode == 'v1' else 'true') and proof[6] == '6.8.0-138-generic' and all(re.fullmatch(r'[0-9a-f]{64}', h) for h in (proof[7], proof[8], proof[11])), 'guest capability proof mismatch')
            require(proof[9:11] == [str(shard), str(total_shards)], 'guest shard environment mismatch')
            require(re.fullmatch('[0-9a-f]{64}', proof[12]) and proof[13] == sha(b'native-string-control\ntail-check\n')
                    and proof[14:] == ['strings_original_absent', selection], 'strings or selection proof mismatch')
            row['guest_proof'] = dict(busybox_sha256=proof[12], strings_control_sha256=proof[13], strings_original_absent=True, cgroup_v2=mode == 'v2', kernel=proof[6], mkfs_sha256=proof[7], loader_sha256=proof[8], erofs_archive_sha256=proof[11], shard_index=int(proof[9]), total_shards=int(proof[10]), docker_absent=True, kvm_present=True)
            save()
        except Exception as exc:
            row['report_error_class'] = type(exc).__name__
            save()
    expected = {('v1', 'annotation'), ('v2', 'annotation'), ('v1', 'missing-shard-4')}
    DOC['missing_attempts'] = [list(v) for v in sorted(expected - seen)]
    save()
    require(seen == expected, 'incomplete guest attempts')
    require(all(not r['local_cache'] and not r['remote_cache'] and r['strategy'] == 'local' for r in DOC['attempts']), 'guest placement mismatch')
    require(all('xml' in r and 'guest_proof' in r for r in DOC['attempts']), 'incomplete guest reports')
    require(all(r['log']['erofs_cli_control'] and r['log']['strings_control'] and not r['log']['setup_failures'] for r in DOC['attempts']), 'guest tool controls did not pass')
    require(code == 0 and all(r['status'] == 'PASSED' and r['xml']['failed'] == 0 and r['xml']['passed'] > 0 for r in DOC['attempts']), 'guest functional failure')
    original = DOC['original']['receipt']['attempts']
    retained_v1 = {name for row in original if row['mode'] == 'v1' and row['owner'] == '//runsc/container:container_test' for name in row['xml']['leaf_cases']}
    for row in DOC['attempts']:
        if row['selection'] == 'annotation':
            require(row['xml']['leaf_cases'] == ['TestCheckpointRestoreAnnotation'] and row['xml']['passed'] == 1
                    and row['xml']['skipped'] == 0, 'focused annotation was not executed alone')
        else:
            require(not retained_v1.intersection(row['xml']['leaf_cases']), 'missing shard repeated an original leaf')
            require('TestMountEROFS' in passed['v1'], 'missing EROFS case did not pass')
    DOC['joined_coverage'] = dict(original_attempts=25, original_passed_attempts=23, original_failed_attempts=2,
        newly_executed_focused_cases=2, newly_executed_original_shards=[['v1', 'container_test', 4]],
        unresolved_original_failure_cases=[], missing_original_attempts=[],
        original_skips=sum(row['xml']['skipped'] for row in original),
        new_skips=sum(row['xml']['skipped'] for row in DOC['attempts']),
        scope='Original passing cases plus two corrected-guest annotation leaves and the missing v1 shard; original failed attempts remain unchanged')
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


def recover_capsules(prior):
    """Materialize the needed original capsule; never substitute a new build."""
    from cas_inputs import KEY, OPENER

    invocation = prior['native_build_evidence']['invocation']
    require(invocation == '4a4c46af-c61d-4d3a-a2a8-a4b11aa6daa6', 'original native invocation mismatch')
    require(set(prior['payloads']) == set(NAMES), 'original capsule set mismatch')
    DOC['capsule_recovery'] = {}
    for name in ('container_test',):
        original = prior['payloads'][name]
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


def recover_guest_downloads():
    """Reuse only the four failed, immutable Debian archives from Ubuntu."""
    from cas_inputs import OPENER

    raw = (TMP / 'fetch-failure.zip').read_bytes()
    require(sha(raw) == '9d52736b31bf09fae2a823afd18d7db547ea9f8d5390f7e897b00dd93ac48bed', 'fetch failure ZIP mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.namelist() == ['container-native-result.json'], 'fetch failure members mismatch')
        prior = json.loads(archive.read('container-native-result.json'))
    require(prior['carrier'] == '312a88ac731277a0e435ec4044432605342a1f51'
            and prior['source'] == SOURCE and prior['product'] == PRODUCT and prior['guest'] == GUEST
            and prior['status'] == 'FAIL' and prior['attempts'] == []
            and prior['commands']['guest-remaining']['exit'] == 1
            and prior['guest_invocations'][0]['invocation'] == '21d4a58c-b13b-4570-b966-6d0ac4397f05', 'unexpected fetch failure')
    DOC['fetch_failure'] = dict(artifact=13267680388, zip_sha256=sha(raw),
        receipt={key: prior[key] for key in ('carrier', 'status', 'commands', 'attempts', 'missing_attempts',
                                            'guest_invocations', 'execution_finished_utc', 'verdict_utc')})
    save()
    require(git('hash-object', 'MODULE.bazel.lock') == git('rev-parse', GUEST + ':MODULE.bazel.lock'), 'guest lock changed')
    lock = json.loads(Path('MODULE.bazel.lock').read_bytes())
    repositories = lock['moduleExtensions']['@@rules_distroless+//apt:extensions.bzl%apt']['general']['generatedRepoSpecs']
    names = ('jammy_libnuma1-amd64_2.0.14-3ubuntu2', 'jammy_libfdt1-amd64_1.6.1-1',
             'jammy_libdaxctl1-amd64_72.1-1', 'jammy-security_libkmod2-amd64_29-1ubuntu1.1')
    directory = TMP / 'guest-deb-distdir'
    directory.mkdir()
    DOC['guest_archive_mirrors'] = []
    for name in names:
        attributes = repositories[name]['attributes']
        require(len(attributes['urls']) == 1 and re.fullmatch('[0-9a-f]{64}', attributes['sha256']), 'invalid pinned archive')
        original = attributes['urls'][0]
        prefix = 'https://snapshot.ubuntu.com/ubuntu/20260928T000000Z/pool/'
        require(original.startswith(prefix), 'unexpected archive origin')
        url = 'https://archive.ubuntu.com/ubuntu/pool/' + original.removeprefix(prefix)
        with OPENER.open(url, timeout=45) as response:
            data = response.read(256 * 1024 + 1)
        require(0 < len(data) <= 256 * 1024 and sha(data) == attributes['sha256'], 'mirror archive differs')
        (directory / url.rsplit('/', 1)[1]).write_bytes(data)
        DOC['guest_archive_mirrors'].append(dict(repository=name, url=url, bytes=len(data), sha256=sha(data)))
        save()
    return directory


def main():
    # Read and bind both immutable originals before using any retained binary.
    import cas_inputs  # Captures the key before prepare removes it from children.

    original = (TMP / 'original.zip').read_bytes()
    require(sha(original) == 'bf9878ea8ec8aea7b72ae576b6156e67d019d797919be514dadbf3bd334000cd', 'original ZIP mismatch')
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        require(archive.namelist() == ['container-native-result.json'], 'original members mismatch')
        prior = json.loads(archive.read('container-native-result.json'))
    require(prior['carrier'] == '1d2d1ca00ecd9c5b25e4d3c9b0506fc2a71ea09c' and prior['status'] == 'INCOMPLETE'
            and prior['source'] == SOURCE and prior['product'] == PRODUCT and prior['guest'] == GUEST
            and prior['commands']['native-build']['exit'] == 0 and prior['commands']['guest']['exit'] == 124
            and prior['phases']['native_make_and_capsules']['status'] == 'PASS', 'unexpected original execution')
    rows = prior['attempts']
    expected = {(mode, '//runsc/container:' + name, shard) for mode in ('v1', 'v2') for name in NAMES for shard in range(SHARDS[name])}
    observed = {(row['mode'], row['owner'], row['shard']) for row in rows}
    require(len(rows) == len(observed) == 25 and expected - observed == {('v1', '//runsc/container:container_test', 4)}
            and observed <= expected, 'original attempt census mismatch')
    failures = [row for row in rows if row['status'] != 'PASSED']
    require(len(failures) == 2 and {(row['mode'], row['owner'], row['shard']) for row in failures} == {
        (mode, '//runsc/container:container_test', 7) for mode in ('v1', 'v2')}, 'original failures differ')
    require(all(row['status'] == 'FAILED' and row['xml']['failed_cases'] == ['TestCheckpointRestoreAnnotation']
                and row['xml']['failed'] == 1 and row['xml']['passed'] == 147 for row in failures), 'original failing case differs')
    require(all('xml' in row and 'guest_proof' in row and row['xml']['passed'] > 0
                and row['strategy'] == 'local' and not row['local_cache'] and not row['remote_cache'] for row in rows), 'original coverage or placement absent')
    require(all(row['xml']['failed'] == 0 for row in rows if row['status'] == 'PASSED'), 'hidden original failure')
    require(prior['boot_gate_passed'] and all({
        'TestMinimalBoot/systrap', 'TestMinimalBoot/kvm', 'TestMinimalBoot/systrap_overlay',
        'TestMinimalBoot/kvm_overlay', 'TestMountEROFSConfig/image1',
    } <= set(prior['platform_cases'][mode]) for mode in ('v1', 'v2'))
        and 'TestMountEROFS' in prior['platform_cases']['v2'], 'original platform coverage absent')
    diagnostic_zip = (TMP / 'diagnostics.zip').read_bytes()
    require(sha(diagnostic_zip) == 'c74e49e78073e92b5899e623f157adbe3d15940ba6714f97930446c4637eddd1', 'diagnostic ZIP mismatch')
    with zipfile.ZipFile(io.BytesIO(diagnostic_zip)) as archive:
        require(archive.namelist() == ['container-diagnostics.json'], 'diagnostic members mismatch')
        diagnostic = json.loads(archive.read('container-diagnostics.json'))
    require(diagnostic['status'] == 'ORIGINAL_LOGS_RECOVERED' and not diagnostic['workload_repeated']
            and diagnostic['original_zip_sha256'] == sha(original), 'diagnostic source mismatch')
    DOC.update(original=dict(artifact=13262889422, zip_sha256=sha(original), receipt=prior),
        original_diagnostics=dict(artifact=13264433003, zip_sha256=sha(diagnostic_zip), receipt=diagnostic),
        scope='Only two original failed annotation leaves and the missing v1 shard, using original binaries with a guest-only strings applet alias',
        product_build_repeated=False, modes=prior['modes'])
    save()
    phase('original_capsule', lambda: recover_capsules(prior))
    require(DOC['phases']['original_capsule']['status'] == 'PASS', 'original capsule unavailable')
    DOC['bazel_version'] = subprocess.check_output(['bazel', '--version'], text=True).strip()
    require(DOC['bazel_version'] == 'bazel ' + Path('.bazelversion').read_text().strip(), 'wrong Bazel')
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
        directory = recover_guest_downloads()
        rc = credential / 'client.bazelrc'
        rc.write_text('common --credential_helper=remote.buildbuddy.io=' + str(credential / 'helper') + '\n'
            'common --distdir=' + str(directory) + '\n'
            'build --bes_backend=grpcs://remote.buildbuddy.io\n'
            'build --bes_results_url=https://app.buildbuddy.io/invocation/\n'
            'build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io\n'
            'build:buildbuddy_remote_executor --remote_cache=grpcs://remote.buildbuddy.io\n'
            'build:buildbuddy_remote_executor --bes_backend=grpcs://remote.buildbuddy.io\n'
            'build:buildbuddy_remote_executor --bes_results_url=https://app.buildbuddy.io/invocation/\n')
        phase('guest_remaining', lambda: guest(rc))
    if any(r['exit'] not in (0, None, 124) and r.get('disposition') != 'VALIDATED_ANALYSIS_ONLY' for r in DOC['commands'].values()) or any(r['status'] == 'FAILED' for r in DOC['attempts']):
        DOC['status'] = 'FAIL'
    elif set(DOC['phases']) == {'original_capsule', 'guest_remaining'} and all(r['status'] == 'PASS' for r in DOC['phases'].values()):
        DOC['status'] = 'RECOVERY_PASS'
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
    sys.exit(0 if DOC['status'] in ('RECOVERY_PASS', 'ORIGINAL_FAIL_DIAGNOSTIC_RECOVERED') else 1)
