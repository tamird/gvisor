"""Retain actual declared atomic caller proof inputs and test evidence."""

import hashlib
import json
from pathlib import Path
import shutil
import shlex
import sys
import zipfile
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')

def source_inputs(expected, arch):
    assert arch in ('amd64', 'arm64')
    return ('pkg/sentry/kernel/fd_table_unsafe.go', 'pkg/sentry/platform/kvm/seccomp_mmap_unsafe.go',
            *expected['generatedSources'], 'pkg/sentry/platform/kvm/machine.go',
            'pkg/sentry/platform/kvm/machine_' + arch + '.go')


def capture_tests(bep, output, start_ms, labels, expected):
    output.mkdir()
    events = []
    errors = []
    if not bep.is_file():
        write(output / 'capture.json', {'missingBEP': True})
        raise AssertionError('Actual BEP missing')
    shutil.copy2(bep, output / 'events.bep')
    for line in bep.read_text().splitlines():
        try:
            item = json.loads(line)
            assert isinstance(item, dict)
            events.append(item)
        except (ValueError, AssertionError) as error:
            errors.append(str(error))
    attempts = [x for x in events if 'testResult' in x]
    summaries = [x for x in events if 'testSummary' in x]
    records = []
    for index, event in enumerate(attempts):
        label = event.get('id', {}).get('testResult', {}).get('label')
        if label not in labels:
            errors.append('Unexpected actual target: ' + str(label))
            continue
        record = {'label': label, 'index': index, 'outputs': {}}
        destination = output / str(index)
        destination.mkdir()
        for item in event['testResult'].get('testActionOutput', []):
            name = item.get('name')
            if name not in ('test.xml', 'test.log', 'test.outputs/amd64-atomicptr-codegen.json', 'test.outputs/arm64-atomicptr-codegen.json'):
                continue
            try:
                uri = urlparse(item['uri'])
                source = Path(unquote(uri.path))
                assert uri.scheme == 'file' and uri.netloc in ('', 'localhost')
                assert str(source).endswith('/' + label[2:].replace(':', '/') + '/' + name)
                assert source.is_file() and not source.is_symlink()
                assert name not in record['outputs']
                shutil.copy2(source, destination / Path(name).name)
                record['outputs'][name] = str(source)
            except (AssertionError, KeyError, OSError, ValueError) as error:
                errors.append(f'{label} {name}: {error}')
        records.append(record)
    write(output / 'capture.json', {'records': records, 'errors': errors})
    reports = {}
    for record in records:
        if record['label'].endswith(':atomicptr_codegen_test'):
            for arch in ('amd64', 'arm64'):
                name = 'test.outputs/' + arch + '-atomicptr-codegen.json'
                if name in record['outputs']:
                    path = output / str(record['index']) / Path(name).name
                    assert path.stat().st_size <= 2 * 1024**2
                    reports[arch] = json.loads(path.read_text())
            write(output / 'actual-inline-reports.json', reports)
    assert not errors, errors
    assert len(attempts) == len(summaries) == len(labels)
    assert {x['id']['testResult']['label'] for x in attempts} == set(labels)
    assert {x['id']['testSummary']['label'] for x in summaries} == set(labels)
    assert all(x['completed'].get('success') is True for x in events if 'completed' in x)
    assert not any('aborted' in x for x in events)
    finished = [x['finished'] for x in events if 'finished' in x]
    assert len(finished) == 1 and finished[0].get('overallSuccess') is True
    assert finished[0].get('exitCode', {}).get('code', 0) == 0
    for attempt, record in zip(attempts, records):
        value = attempt['testResult']
        assert value['status'] == 'PASSED'
        assert value.get('cachedLocally', False) is False and value.get('cachedRemotely', False) is False
        assert value.get('executionInfo', {}).get('cachedRemotely', False) is False
        began = int(value['testAttemptStartMillisEpoch'])
        assert began >= start_ms
        summary = next(x['testSummary'] for x in summaries if x['id']['testSummary']['label'] == record['label'])
        assert summary['overallStatus'] == 'PASSED' and summary['totalRunCount'] == 1 and summary.get('totalNumCached', 0) == 0
        assert {'test.xml', 'test.log'} <= set(record['outputs'])
        xml = output / str(record['index']) / 'test.xml'
        assert xml.stat().st_mtime_ns // 1_000_000 >= start_ms
        root = ET.parse(xml).getroot()
        cases = list(root.iter('testcase'))
        assert cases
        assert all(case.get('status', 'run') == 'run' and case.get('result', 'completed') == 'completed' for case in cases)
        assert all(all(case.find(kind) is None for kind in ('failure', 'error', 'skipped')) for case in cases)
        for suite in root.iter('testsuite'):
            assert all(int(suite.get(kind, '0')) == 0 for kind in ('failures', 'errors', 'skipped'))
        names = [case.get('name') for case in cases]
        assert len(names) == len(set(names))
        assert set(expected.get('requiredCases', {}).get(record['label'], [])) <= set(names), names
        required = expected['requiredTopLevelCases'][record['label']]
        if required:
            assert {name.split('/')[0] for name in names} == set(required), names
        else:
            assert record['label'] in expected['goldenTargets']
        if record['label'].endswith(':atomicptr_codegen_test'):
            assert set(names) == {'TestAtomicPtrProductionCodegen', 'TestAtomicPtrProductionCodegen/amd64', 'TestAtomicPtrProductionCodegen/arm64'}, names
            assert set(reports) == {'amd64', 'arm64'}, 'Exact per-architecture BEP JSON outputs required'
            for arch in ('amd64', 'arm64'):
                report = reports[arch]
                assert report['arch'] == arch and report['sdk'] == 'go1.27.3'
                assert report['positiveInlineAndCallsPassed'] is True and report['realNoInlineControlRejected'] is True
                assert len(report['requiredSites']) == 7 and report['liveInlineRecords']
                required_sites = report['requiredSites']
                assert required_sites[0]['Receiver'] == 'machinePool[i]' and required_sites[0]['Method'] == 'Load'
                assert required_sites[0]['File'] == 'pkg/sentry/platform/kvm/seccomp_mmap_unsafe.go'
                if arch == 'arm64':
                    assert report['applicableSites'] == required_sites[1:]
                    assert report['notApplicableSites'] == required_sites[:1]
                    assert report['notApplicableReason'] == expected['armHandlerNotApplicableReason']
                else:
                    assert report['applicableSites'] == required_sites
                    assert not report.get('notApplicableSites') and not report.get('notApplicableReason')
                logical = source_inputs(expected, arch)
                assert set(report['sourceSHA256']) == set(logical)
                assert all(report['sourceSHA256'][name] == expected['sourceSHA256'][name]
                           for name in logical if name not in expected['generatedSources'])
                assert all(isinstance(digest, str) and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest) for digest in report['sourceSHA256'].values())

        record['actualCases'] = names
    write(output / 'verified.json', {'passed': True, 'records': records})


def capture_products(manifest_paths, binary_paths, output, expected, reports_path):
    output.mkdir()
    manifests = [Path(x) for x in manifest_paths.read_text().splitlines() if x.strip()]
    binaries = [Path(x) for x in binary_paths.read_text().splitlines() if x.strip()]
    assert len(manifests) == len(binaries) == 1
    manifest = json.loads(manifests[0].read_text())
    assert set(manifest) == {'go', 'sdk', 'records'} and manifest['sdk'] == 'go1.27.3'
    assert len(manifest['records']) == 2
    shutil.copy2(manifests[0], output / 'inputs.json')
    runfiles = Path(str(binaries[0]) + '.runfiles') / '_main'
    assert runfiles.is_dir(), runfiles
    seen = set()
    records = []
    for record in manifest['records']:
        arch = record['arch']
        assert arch in ('amd64', 'arm64') and arch not in seen
        seen.add(arch)
        prefix = f'pkg/sentry/platform/kvm/atomicptr_codegen_inputs/{arch}/'
        assert set(record['artifacts']) == {'elf', 'kernel_archive', 'kvm_archive', 'negative_archive'}
        directory = output / arch
        directory.mkdir()
        for name, alias in record['artifacts'].items():
            assert alias == prefix + name
            source = runfiles / alias
            assert source.is_file()
            target = directory / name
            shutil.copy2(source, target)
            records.append({'arch': arch, 'name': name, 'size': target.stat().st_size,
                            'sha256': digest(target), 'resolvedOriginalPath': str(source.resolve())})
        logical = source_inputs(expected, arch)
        assert len(record['sources']) == len(logical)
        assert set(record['sources']) == {prefix + 'sources/' + name for name in logical}
        for index, name in enumerate(logical):
            source = runfiles / (prefix + 'sources/' + name)
            target = directory / (str(index) + '-' + Path(name).name)
            shutil.copy2(source, target)
            records.append({'arch': arch, 'source': name, 'size': target.stat().st_size,
                            'sha256': digest(target), 'resolvedOriginalPath': str(source.resolve())})
    if reports_path.is_file():
        reports = json.loads(reports_path.read_text())
        for arch in ('amd64', 'arm64'):
            sources = {item['source']: item['sha256'] for item in records if item['arch'] == arch and 'source' in item}
            artifacts = {item['name']: item['sha256'] for item in records if item['arch'] == arch and 'name' in item}
            assert reports[arch]['sourceSHA256'] == sources
            assert reports[arch]['artifactSHA256'] == artifacts
    write(output / 'verified.json', {'declaredInputsCaptured': True, 'records': records,
                                    'scope': 'Code-inspection inputs, not an installed runtime fileset; no runtime executed'})


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            value.update(block)
    return value.hexdigest()


def declared_paths(bep, output, expected):
    events = [json.loads(line) for line in bep.read_bytes().splitlines()]
    label = '//pkg/sentry/platform/kvm:atomicptr_codegen_test'
    owners = [event for event in events if 'completed' in event
              and event.get('id', {}).get('targetCompleted', {}).get('label') == label
              and not event['id']['targetCompleted'].get('aspect')]
    assert len(owners) == 1 and owners[0]['completed'].get('success') is True
    config = owners[0]['id']['targetCompleted']['configuration']['id']
    for kind in ('testResult', 'testSummary'):
        matches = [event for event in events if kind in event and event.get('id', {}).get(kind, {}).get('label') == label]
        assert len(matches) == 1 and matches[0]['id'][kind]['configuration']['id'] == config
    sets = {}
    for event in events:
        if 'namedSetOfFiles' in event:
            key = event['id']['namedSet']['id']
            assert key not in sets
            sets[key] = event['namedSetOfFiles']
    groups = [group for group in owners[0]['completed']['outputGroup'] if group['name'] == 'default']
    assert len(groups) == 1
    files = []
    visited = set()
    def visit(key, stack):
        assert key not in stack
        if key in visited:
            return
        visited.add(key)
        value = sets[key]
        files.extend(value.get('files', []))
        for child in value.get('fileSets', []):
            visit(child['id'], stack | {key})
    for child in groups[0].get('fileSets', []):
        visit(child['id'], set())
    binaries = []
    for item in files:
        uri = urlparse(item.get('uri', ''))
        if uri.scheme == 'file' and uri.netloc in ('', 'localhost'):
            path = Path(unquote(uri.path))
            if path.name == 'atomicptr_codegen_test':
                binaries.append(path)
    assert len(binaries) == 1 and binaries[0].is_file()
    manifest = Path(str(binaries[0]) + '.runfiles') / '_main/pkg/sentry/platform/kvm/atomicptr_codegen_inputs.json'
    assert manifest.is_file()
    (output / 'proof-binary.paths').write_text(str(binaries[0]) + '\n')
    (output / 'inputs.paths').write_text(str(manifest) + '\n')
    write(output / 'configured-product-owner.json', {'label': label, 'configuration': config, 'binary': str(binaries[0]), 'manifest': str(manifest)})


def capture_actions(path, output, products, execroot):
    graph = json.loads(path.read_text())
    retained = json.loads(products.read_text())['records']
    targets = {item['id']: item['label'] for item in graph['targets']}
    fragments = {item['id']: item for item in graph['pathFragments']}
    def fragment(key, stack=None):
        stack = set() if stack is None else stack
        assert key not in stack
        value = fragments[key]
        return ((fragment(value['parentId'], stack | {key}) + '/') if value.get('parentId') else '') + value['label']
    artifacts = {item['id']: fragment(item['pathFragmentId']) for item in graph['artifacts']}
    depsets = {item['id']: item for item in graph['depSetOfFiles']}
    input_closures = {}
    def inputs(key, stack=None):
        stack = set() if stack is None else stack
        assert key not in stack
        if key in input_closures:
            return input_closures[key]
        value = depsets[key]
        result = {artifacts[item] for item in value.get('directArtifactIds', [])}
        for child in value.get('transitiveDepSetIds', []):
            result |= inputs(child, stack | {key})
        input_closures[key] = frozenset(result)
        return input_closures[key]
    def arguments(action):
        params = {item['execPath']: item['arguments'] for item in action.get('paramFiles', [])}
        result = []
        for item in action['arguments']:
            if item.startswith('-param='):
                result.extend(params[item.removeprefix('-param=')])
            else:
                result.append(item)
        assert not any(item.startswith('-param=') for item in result)
        return result
    def values(args, flag):
        result = []
        for index, arg in enumerate(args):
            if arg == flag:
                result.append(args[index + 1])
            elif arg.startswith(flag + '='):
                result.append(arg[len(flag) + 1:])
        return result
    records = []
    for arch in ('amd64', 'arm64'):
        actual = {item['name']: item for item in retained if item['arch'] == arch and 'name' in item}
        def matches(original, artifact):
            return (execroot / artifact).resolve() == Path(original).resolve()
        links = [action for action in graph['actions'] if action['mnemonic'] == 'GoLink'
                 and targets[action['targetId']] == '//runsc/cmd/sentry:gvisor_sentry'
                 and any(matches(actual['elf']['resolvedOriginalPath'], artifacts[key]) for key in action['outputIds'])]
        assert len(links) == 1, links
        link = links[0]
        assert digest(Path(actual['elf']['resolvedOriginalPath'])) == actual['elf']['sha256']
        argv = arguments(link)
        assert values(argv, '-linkmode') == ['internal']
        assert not {'-s', '-w', '-race', '-msan', '-cover'} & set(argv)
        closure = set()
        for key in link.get('inputDepSetIds', []):
            closure |= inputs(key)
        mappings = [item.split('=', 2) for item in values(argv, '-arc')]
        selected = []
        for name, owner, importmap in (
            ('kernel_archive', '//pkg/sentry/kernel:kernel', 'gvisor.dev/gvisor/pkg/sentry/kernel'),
            ('kvm_archive', '//pkg/sentry/platform/kvm:kvm', 'gvisor.dev/gvisor/pkg/sentry/platform/kvm'),
            ('negative_archive', '//pkg/sentry/platform/kvm:atomicptr_noinline', 'gvisor.dev/gvisor/pkg/sentry/platform/kvm')):
            item = actual[name]
            paths = [value for value in artifacts.values() if matches(item['resolvedOriginalPath'], value)]
            assert len(set(paths)) == 1
            archive_path = paths[0]
            assert digest(execroot / archive_path) == item['sha256']
            if name != 'negative_archive':
                assert archive_path in closure
                matching = [mapping for mapping in mappings if len(mapping) == 3 and mapping[1] == importmap]
                assert len(matching) == 1 and matching[0][2] == archive_path
            else:
                assert archive_path not in closure
            producers = [action for action in graph['actions'] if action['mnemonic'] == 'GoCompilePkg'
                         and targets[action['targetId']] == owner and archive_path in [artifacts[key] for key in action['outputIds']]]
            assert len(producers) == 1
            producer = producers[0]
            args = arguments(producer)
            assert values(args, '-lo') == [archive_path] and values(args, '-p') == [importmap]
            environment = {value['key']: value.get('value', '') for value in producer['environmentVariables']}
            assert environment['GOOS'] == 'linux' and environment['GOARCH'] == arch
            assert environment['CGO_ENABLED'] == '0'
            flags = [flag for value in values(args, '-gcflags') for flag in shlex.split(value)]
            forbidden = {'-N', '-race', '-msan', '-asan'}
            assert not forbidden & set(flags)
            assert ('-l' in flags) is (name == 'negative_archive')
            assert not values(args, '-cover') and not values(args, '-cover_mode')
            sources = [source for source in retained if source['arch'] == arch and 'source' in source
                       and source['source'].startswith('pkg/sentry/' + ('kernel/' if name == 'kernel_archive' else 'platform/kvm/'))]
            assert sources
            for source in sources:
                declared = [value for value in values(args, '-src') if Path(value).name == Path(source['source']).name]
                assert len(declared) == 1
                assert digest(execroot / declared[0]) == source['sha256']
                if name != 'negative_archive':
                    assert matches(source['resolvedOriginalPath'], declared[0])
            selected.append({'name': name, 'path': archive_path, 'producer': producer, 'expandedArguments': args})
        records.append({'arch': arch, 'link': link, 'expandedLinkArguments': argv, 'selectedArchives': selected})
    write(output, {'actualProductionArchiveBindingsPassed': True, 'records': records})


if __name__ == '__main__':
    mode, *args = sys.argv[1:]
    if mode == 'tests':
        bep, output, started, expected, *labels = args
        capture_tests(Path(bep), Path(output), int(started), labels, json.loads(Path(expected).read_text()))
    elif mode == 'products':
        manifest, binary, output, expected, reports = args
        capture_products(Path(manifest), Path(binary), Path(output), json.loads(Path(expected).read_text()), Path(reports))
    elif mode == 'declared':
        bep, output, expected = args
        declared_paths(Path(bep), Path(output), json.loads(Path(expected).read_text()))
    else:
        assert mode == 'actions' and len(args) == 4
        capture_actions(*(Path(x) for x in args))
