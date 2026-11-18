"""Retain and validate the semaphore owner qualification's declared evidence."""

import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def artifact(item, label, name):
    uri = urlparse(item['uri'])
    assert uri.scheme == 'file' and uri.netloc in ('', 'localhost'), item
    path = Path(unquote(uri.path))
    assert str(path).endswith('/' + label[2:].replace(':', '/') + '/' + name), path
    assert path.is_file() and not path.is_symlink(), path
    return path


def tests(state, bep, output, expected, invocation_start_ms, labels):
    assert type(invocation_start_ms) is int and invocation_start_ms > 0
    assert state in ('passed', 'failed')
    assert labels and len(labels) == len(set(labels)), labels
    allowed = set(expected['nogoTargets']) | {expected['nativeTarget']}
    assert set(labels) <= allowed, labels
    if state == 'failed':
        assert labels == [expected['nogoTargets'][0]], labels
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(bep, output / 'events.bep')
    events = []
    parse_errors = []
    for number, line in enumerate(bep.read_bytes().splitlines(), 1):
        try:
            event = json.loads(line)
            assert isinstance(event, dict), 'BEP event is not an object'
            events.append(event)
        except (ValueError, AssertionError) as error:
            parse_errors.append({'line': number, 'error': str(error)})
    attempts = [event for event in events if 'testResult' in event]
    summaries = [event for event in events if 'testSummary' in event]
    finished = [event['finished'] for event in events if 'finished' in event]
    # Archive every available requested owner's XML/log before status checks.
    records = []
    capture_errors = []
    for index, event in enumerate(attempts):
        label = event.get('id', {}).get('testResult', {}).get('label')
        if label not in labels:
            capture_errors.append({'unexpectedTarget': label})
            continue
        directory = output / str(index)
        directory.mkdir()
        record = {'label': label, 'directory': str(index), 'outputs': {}}
        for name in ('test.xml', 'test.log'):
            items = [item for item in event['testResult'].get('testActionOutput', []) if item.get('name') == name]
            try:
                assert len(items) == 1, (label, name, items)
                source = artifact(items[0], label, name)
                shutil.copy2(source, directory / name)
                record['outputs'][name] = str(source)
            except (AssertionError, OSError, KeyError, ValueError) as error:
                capture_errors.append({'label': label, 'artifact': name, 'error': str(error)})
        records.append(record)
    write(output / 'captured.json', {'targets': records, 'errors': capture_errors, 'parseErrors': parse_errors, 'invocationStartMillis': invocation_start_ms})
    assert not parse_errors and not capture_errors, (parse_errors, capture_errors)
    assert not any('aborted' in event for event in events), 'Aborted BEP event'
    # TargetComplete describes build success, independently of TestResult.
    # Bazel 9.3.1 TargetCompleteEvent.successfulBuildSchedulingTest emits this
    # successful build event before the intentionally failing old-checker test.
    completions = [event for event in events if 'completed' in event]
    assert all(event['completed'].get('success', False) is True for event in completions), completions
    for label in labels:
        normal = [event for event in completions if event.get('id', {}).get('targetCompleted', {}).get('label') == label and not event['id']['targetCompleted'].get('aspect')]
        assert len(normal) == 1, (label, normal)
    assert len(attempts) == len(summaries) == len(labels)
    assert {event['id']['testResult']['label'] for event in attempts} == set(labels)
    assert {event['id']['testSummary']['label'] for event in summaries} == set(labels)
    assert len(finished) == 1, finished
    success = state == 'passed'
    assert finished[0].get('overallSuccess', False) is success, finished
    assert finished[0]['exitCode'].get('code', 0) == (0 if success else 3), finished
    status = 'PASSED' if success else 'FAILED'
    for event, record in zip(attempts, records):
        result = event['testResult']
        assert result['status'] == status, result
        assert result.get('cachedLocally', False) is False
        assert result.get('cachedRemotely', False) is False
        assert result.get('executionInfo', {}).get('cachedRemotely', False) is False
        started = int(result['testAttemptStartMillisEpoch'])
        assert started >= invocation_start_ms, (started, invocation_start_ms)
        label = record['label']
        summary = next(event['testSummary'] for event in summaries if event['id']['testSummary']['label'] == label)
        assert summary['overallStatus'] == status and summary['totalRunCount'] == 1, summary
        assert summary.get('totalNumCached', 0) == 0, summary
        directory = output / record['directory']
        xml = directory / 'test.xml'
        assert xml.stat().st_mtime_ns // 1_000_000 >= started - 1000
        root = ET.parse(xml).getroot()
        cases = list(root.iter('testcase'))
        assert cases, label
        assert all(case.get('status', 'run') == 'run' and case.get('result', 'completed') == 'completed' for case in cases), [case.attrib for case in cases]
        skipped = [case.get('name') for case in cases if case.find('skipped') is not None or case.get('status') == 'notrun' or case.get('result') == 'suppressed']
        failures = [case.get('name') for case in cases if case.find('failure') is not None or case.find('error') is not None]
        assert not skipped, skipped
        for suite in root.iter('testsuite'):
            assert int(suite.get('skipped', '0')) == 0
            if success:
                assert int(suite.get('failures', '0')) == int(suite.get('errors', '0')) == 0
        if success:
            assert not failures, (label, failures)
        else:
            assert failures, (label, failures)
        names = [case.get('name') for case in cases]
        record.update(cases=names, failureCases=failures, skippedCases=skipped)
        if label == expected['nativeTarget']:
            assert len(names) == len(set(names)) and set(names) == set(expected['nativeCases']), names
            raw = (directory / 'test.log').read_text().replace('\x16', '')
            observations = re.findall(r'sem size=(\d+); encoded state bytes=(\d+)', raw)
            assert len(observations) == 1, observations
            size, encoded = map(int, observations[0])
            assert size > 0 and encoded > 0
            record['stateRoundTripObservation'] = {'semSizeBytes': size, 'encodedStateBytes': encoded, 'sizeOrPerformanceNeutralityClaim': False}
    write(output / 'verified.json', {'state': state, 'source': expected['source'], 'control': expected['base'], 'targets': records})



def configuration(bep, label, expected):
    """Bind output lookup to the exact configured owner tested in this BEP."""
    assert label in expected['nogoTargets'], label
    raw = bep.read_bytes()
    events = [json.loads(line) for line in raw.splitlines()]
    assert all(isinstance(event, dict) for event in events)
    owners = [event for event in events if 'completed' in event
              and event.get('id', {}).get('targetCompleted', {}).get('label') == label
              and not event['id']['targetCompleted'].get('aspect')]
    assert len(owners) == 1 and owners[0]['completed'].get('success') is True, owners
    identifier = owners[0]['id']['targetCompleted']['configuration']['id']
    assert re.fullmatch(r'[a-f0-9]{64}', identifier), identifier
    configurations = [event for event in events if 'configuration' in event
                      and event.get('id', {}).get('configuration', {}).get('id') == identifier]
    assert len(configurations) == 1, configurations
    for kind in ('testResult', 'testSummary'):
        records = [event for event in events if kind in event
                   and event.get('id', {}).get(kind, {}).get('label') == label]
        assert len(records) == 1, (kind, records)
        assert records[0]['id'][kind]['configuration']['id'] == identifier, (kind, records)
    return {'label': label, 'configuration': identifier,
            'bepSHA256': hashlib.sha256(raw).hexdigest(),
            'query': 'config(' + label + ', ' + identifier + ')'}

def findings(mode, pathlist, output, expected, label, binding):
    assert mode in ('source', 'old')
    assert binding['label'] == label
    assert re.fullmatch(r'[a-f0-9]{64}', binding['configuration'])
    assert binding['query'] == 'config(' + label + ', ' + binding['configuration'] + ')'
    assert label in expected['nogoTargets'], label
    if mode == 'old':
        assert label == expected['nogoTargets'][0], label
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(pathlist, output / 'declared-paths.txt')
    paths = [Path(line) for line in pathlist.read_text().splitlines() if line.strip()]
    arches = expected['analyzedArchitectures']
    assert len(paths) == len(arches) == 2 and len(set(paths)) == 2, paths
    short = label.split(':')[1]
    package = label[2:].split(':')[0]
    copied = {}
    for path in paths:
        assert path.is_file() and not path.is_symlink(), path
        matches = [arch for arch in arches if str(path).endswith('/' + package + '/' + short + '.' + arch + '.findings')]
        assert len(matches) == 1, path
        arch = matches[0]
        assert arch not in copied, arch
        shutil.copy2(path, output / (arch + '.findings'))
        copied[arch] = path
    assert set(copied) == set(arches)
    records = {}
    for arch in arches:
        value = json.loads((output / (arch + '.findings')).read_text())
        if value is None:
            value = []
        assert isinstance(value, list), value
        for item in value:
            assert isinstance(item, dict), item
            assert set(item) == {'Category', 'Position', 'Message', 'GOARCH', 'GOOS'}, item
            assert isinstance(item['Category'], str) and isinstance(item['Message'], str), item
            assert item['GOARCH'] == arch and item['GOOS'] == 'linux', item
            position = item['Position']
            assert isinstance(position, dict) and set(position) == {'Filename', 'Offset', 'Line', 'Column'}, position
            assert isinstance(position['Filename'], str)
            assert all(type(position[key]) is int and position[key] >= 0 for key in ('Offset', 'Line', 'Column')), position
        if mode == 'source':
            assert not value, (label, arch, value)
            records[arch] = {'findings': 0}
        else:
            anchor = expected['oldCheckerPositiveControl']
            matched = [item for item in value if item['Category'] == anchor['category'] and (item['Position']['Filename'] == anchor['path'] or item['Position']['Filename'].endswith('/' + anchor['path'])) and item['Position']['Line'] == anchor['line'] and anchor['messageContains'] in item['Message']]
            assert matched, (arch, anchor, value)
            records[arch] = {'findings': len(value), 'knownValidOwnerRejected': matched}
    write(output / 'verified.json', {'mode': mode, 'label': label, 'source': expected['source'], 'control': expected['base'], 'architectures': records, 'configuredOwner': binding})


def main():
    if sys.argv[1] == 'configuration':
        _, bep, output, expected_path, label = sys.argv[1:]
        binding = configuration(Path(bep), label, json.loads(Path(expected_path).read_text()))
        write(Path(output), binding)
        print(binding['configuration'])
        return
    command, mode, input_path, output_path, expected_path, *labels = sys.argv[1:]
    expected = json.loads(Path(expected_path).read_text())
    if command == 'tests':
        invocation_start_ms, *labels = labels
        tests(mode, Path(input_path), Path(output_path), expected, int(invocation_start_ms), labels)
    else:
        assert command == 'findings' and len(labels) == 2
        findings(mode, Path(input_path), Path(output_path), expected, labels[0], json.loads(Path(labels[1]).read_text()))


if __name__ == '__main__':
    main()
