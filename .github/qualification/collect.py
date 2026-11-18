"""Validate real semaphore consumer receipts; never copy checkpoint payloads."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET
import zipfile


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            value.update(block)
    return value.hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def xml_cases(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter('testcase'))
    assert cases, 'Empty actual GTest XML'
    for suite in root.iter('testsuite'):
        assert all(int(suite.get(key, '0')) == 0 for key in ('failures', 'errors', 'skipped', 'disabled'))
    result = []
    for case in cases:
        assert case.get('status', 'run') == 'run' and case.get('result', 'completed') == 'completed'
        assert all(case.find(key) is None for key in ('failure', 'error', 'skipped'))
        assert case.get('classname') and case.get('name')
        result.append(case.get('classname') + '.' + case.get('name'))
    assert len(result) == len(set(result)), result
    return result


def local_file(item):
    uri = urlparse(item['uri'])
    assert uri.scheme == 'file' and uri.netloc in ('', 'localhost')
    path = Path(unquote(uri.path))
    assert path.is_file() and not path.is_symlink(), path
    return path


def configured_owner(events, label):
    matches = [event for event in events if 'completed' in event
               and event.get('id', {}).get('targetCompleted', {}).get('label') == label
               and not event['id']['targetCompleted'].get('aspect')]
    assert len(matches) == 1 and matches[0]['completed'].get('success') is True
    identifier = matches[0]['id']['targetCompleted']['configuration']['id']
    assert re.fullmatch(r'[a-f0-9]{64}', identifier)
    return matches[0], identifier


def default_files(events, owner):
    sets = {}
    for event in events:
        if 'namedSetOfFiles' in event:
            key = event['id']['namedSet']['id']
            assert key not in sets
            sets[key] = event['namedSetOfFiles']
    groups = [group for group in owner['completed'].get('outputGroup', []) if group['name'] == 'default']
    assert len(groups) == 1
    seen, files = set(), []
    def visit(key, stack):
        assert key not in stack
        if key in seen:
            return
        seen.add(key)
        value = sets[key]
        files.extend(value.get('files', []))
        for child in value.get('fileSets', []):
            visit(child['id'], stack | {key})
    for child in groups[0].get('fileSets', []):
        visit(child['id'], set())
    return files


def runtime_commands(text, runtime, phase):
    commands = [line.split('Executing: [', 1)[1].split(']', 1)[0]
                for line in text.splitlines() if 'Executing: [' + runtime + ' ' in line]
    assert commands, 'No actual source-bound runtime command'
    for command in commands:
        assert re.search(r'-platform(?:=|\s+)systrap(?:\s|$)', command), command
        assert re.search(r'-directfs(?:=true)?(?:\s|$)', command), command
    saved = [command for command in commands if '-TESTONLY-autosave-image-path' in command]
    resumed = [command for command in saved if '-TESTONLY-autosave-resume=true' in command]
    restored = [command for command in commands if re.search(r'\srestore\s', command) and '--image-path' in command]
    if phase == 'ordinary':
        assert not saved and not resumed and not restored
    else:
        assert saved
        if phase == 'save-restore':
            assert restored and not resumed, 'No actual restore command'
        else:
            assert phase == 'save-resume' and resumed and not restored
    return {'actualRuntimeCommands': commands, 'actualRestoreCommands': len(restored), 'actualAutosaveCommands': len(saved), 'actualResumeCommands': len(resumed)}


def capture(phase, results, expected, runtime):
    label = expected['nativeBinary'] if phase == 'native' else expected['sentryTargets'][('ordinary', 'save-restore', 'save-resume').index(phase)]
    destination = results / (phase + '-evidence')
    destination.mkdir()
    bep = results / (phase + '.bep')
    assert bep.is_file()
    shutil.copy2(bep, destination / 'events.bep')
    raw = bep.read_bytes()
    events = []
    parse_errors = []
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            event = json.loads(line)
            assert isinstance(event, dict)
            events.append(event)
        except (ValueError, AssertionError) as error:
            parse_errors.append({'line': number, 'error': str(error)})
    start_ms = int((results / (phase + '-start-ms.txt')).read_text())
    actual_exit = int((results / (phase + '-exit.txt')).read_text())
    capture_errors = []
    attempts = [event for event in events if 'testResult' in event]
    if phase == 'native':
        xml = results / 'native.xml'
        if xml.is_file():
            shutil.copy2(xml, destination / 'test.xml')
        else:
            capture_errors.append('Native actual GTestXML missing')
        assert not attempts, 'Bazelrun is not a Bazel testResult'
    else:
        # Copy only XML and text log immediately; outputs.zip may contain state.
        for event in attempts:
            if event.get('id', {}).get('testResult', {}).get('label') != label:
                capture_errors.append('Unexpected test owner')
                continue
            outputs = event['testResult'].get('testActionOutput', [])
            for name in ('test.xml', 'test.log'):
                try:
                    items = [item for item in outputs if item.get('name') == name]
                    assert len(items) == 1
                    path = local_file(items[0])
                    assert str(path).endswith('/' + label[2:].replace(':', '/') + '/' + name)
                    shutil.copy2(path, destination / name)
                except (AssertionError, KeyError, ValueError, OSError) as error:
                    capture_errors.append(str(error))
            archives = [item for item in outputs if item.get('name') == 'test.outputs/outputs.zip']
            if archives:
                assert len(archives) == 1
                path = local_file(archives[0])
                with zipfile.ZipFile(path) as archive:
                    members = archive.infolist()
                    assert len({item.filename for item in members}) == len(members)
                    write(destination / 'undeclared-output-metadata.json', {'members': len(members), 'logicalBytes': sum(item.file_size for item in members), 'payloadCopiedOrUploaded': False})
    write(destination / 'capture.json', {'source': expected['source'], 'label': label, 'bepSHA256': hashlib.sha256(raw).hexdigest(), 'commandExit': actual_exit, 'errors': capture_errors, 'parseErrors': parse_errors})
    assert not capture_errors and not parse_errors, (capture_errors, parse_errors)
    owner, config = configured_owner(events, label)
    assert actual_exit == 0
    assert not any('aborted' in event for event in events)
    assert all(event['completed'].get('success') is True for event in events if 'completed' in event)
    finished = [event['finished'] for event in events if 'finished' in event]
    assert len(finished) == 1 and finished[0].get('overallSuccess') is True and finished[0].get('exitCode', {}).get('code', 0) == 0
    xml = destination / 'test.xml'
    assert xml.stat().st_mtime_ns // 1_000_000 >= start_ms
    cases = xml_cases(xml)
    assert len(cases) == 30 and set(cases) == set(expected['cases']), cases
    config_proof = {}
    if phase == 'native':
        candidates = [local_file(item) for item in default_files(events, owner)
                      if Path(unquote(urlparse(item.get('uri', '')).path)).name == 'semaphore_test']
        assert len(candidates) == 1
        binary = candidates[0]
        config_proof = {'canonicalBinary': str(binary), 'binarySHA256': digest(binary), 'nativeRunBEPNotTestResult': True}
    if phase != 'native':
        summaries = [event for event in events if 'testSummary' in event]
        assert len(attempts) == len(summaries) == 1
        event = attempts[0]
        assert event['id']['testResult']['configuration']['id'] == config
        value = event['testResult']
        assert value['status'] == 'PASSED'
        assert not value.get('cachedLocally', False) and not value.get('cachedRemotely', False) and not value.get('executionInfo', {}).get('cachedRemotely', False)
        assert int(value['testAttemptStartMillisEpoch']) >= start_ms
        summary = summaries[0]
        assert summary['id']['testSummary']['label'] == label and summary['id']['testSummary']['configuration']['id'] == config
        assert summary['testSummary']['overallStatus'] == 'PASSED' and summary['testSummary']['totalRunCount'] == 1 and summary['testSummary'].get('totalNumCached', 0) == 0
        files = [item for item in default_files(events, owner) if Path(unquote(urlparse(item.get('uri', '')).path)).name == label.split(':')[1]]
        assert len(files) == 1
        wrapper = local_file(files[0])
        text = wrapper.read_text()
        shutil.copy2(wrapper, destination / 'canonical-wrapper.sh')
        for value in ('--platform=systrap', '--directfs=True', '--network=none'):
            assert value in text, (value, text)
        assert '--save=' + ('True' if phase == 'save-restore' else 'False') in text
        assert '--save-resume=' + ('True' if phase == 'save-resume' else 'False') in text
        log = (destination / 'test.log').read_text()
        assert 'WARNING: DATA RACE' not in log, 'Actual race detector report'
        config_proof = runtime_commands(log, runtime, phase)
    write(destination / 'verified.json', {'source': expected['source'], 'label': label, 'configuration': config, 'cases': cases, 'runtimeProof': config_proof, 'nativeBazelTestResult': False if phase == 'native' else None, 'checkpointPayloadUploaded': False})


if __name__ == '__main__':
    phase, results, expected, runtime = sys.argv[1:]
    capture(phase, Path(results), json.loads(Path(expected).read_text()), runtime)
