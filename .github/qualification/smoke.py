"""Run one fresh canonical owner test and retain its actual BEP/XML immediately."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

from owned_commands import Commands
from container_identity import intent, complete
from validate import require_fresh

def archive_available(bep, destination, target, began_ms):
    receipt = {'bepExists': bep.is_file(), 'xmlCopied': False, 'errors': []}
    events = []
    if bep.is_file():
        for line in bep.read_text().splitlines():
            try:
                event = json.loads(line)
                events.append(event)
                if event.get('id', {}).get('testResult', {}).get('label') != target:
                    continue
                for item in event.get('testResult', {}).get('testActionOutput', []):
                    if item.get('name') != 'test.xml':
                        continue
                    uri = urlparse(item['uri'])
                    xml = Path(unquote(uri.path))
                    assert uri.scheme == 'file' and uri.netloc in ('', 'localhost')
                    assert str(xml).endswith('/test/benchmarks/fs/fsbench/fsbench_test/test.xml')
                    assert xml.is_file() and not xml.is_symlink()
                    require_fresh(xml.stat().st_mtime_ns // 1_000_000, began_ms)
                    shutil.copy2(xml, destination / 'test.xml')
                    receipt['xmlCopied'] = True
            except (AssertionError, KeyError, TypeError, ValueError, OSError) as error:
                receipt['errors'].append(type(error).__name__ + ': ' + str(error))
    (destination / 'available-evidence.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return events, receipt


work, results, support = map(Path, sys.argv[1:4])
label, runtime = sys.argv[4:6]
commands = Commands(results / 'smoke-active-groups.json')

def command(args, timeout=10):
    return commands.command(args, timeout=timeout)

assert label in ('native', 'base', 'candidate')
assert runtime == ('runc' if label == 'native' else 'vfio-gofer-pilot')
target = '//test/benchmarks/fs/fsbench:fsbench_test'
assert command(['git', 'rev-parse', 'HEAD']).strip() == os.environ['SOURCE']
destination = results / ('owner-' + label)
destination.mkdir(mode=0o700)
bep = destination / 'events.bep'
image = json.loads((results / 'image-basic-alpine.json').read_text())[0]['Id']
assert command(['sudo', 'docker', 'image', 'inspect', '--format', '{{.Id}}', 'gvisor.dev/images/basic/alpine:latest'], timeout=5).strip() == image
access = command(['docker', '--host=unix:///var/run/docker.sock', 'version', '--format', '{{.Server.Version}}'])
(destination / 'worker-docker-access.txt').write_text(access)
assert not command(['sudo', 'docker', 'ps', '-aq'], timeout=5).split()
args = ['bazel', '--host_jvm_args=-Xmx4g', '--nosystem_rc', '--nohome_rc', 'test',
        '--config=x86_64', '--jobs=3', '--nostamp', '--enable_runfiles',
        '--remote_cache=', '--remote_executor=', '--bes_backend=',
        '--build_event_json_file=' + str(bep), '--nocache_test_results',
        '--flaky_test_attempts=1', '--strategy=TestRunner=local', '--test_timeout=90',
        '--test_output=errors', '--test_env=GO_TEST_WRAP_TESTV=1', '--test_env=RUNTIME=' + runtime,
        '--test_env=DOCKER_HOST=unix:///var/run/docker.sock', target]
(destination / 'argv.json').write_text(json.dumps(args, indent=2) + '\n')
began_ms = time.time_ns() // 1_000_000
intent(results, 'owner-' + label, os.environ['SOURCE'], image, runtime, r'/[A-Z2-7]{32}', began_ms / 1000)
began = time.monotonic()
primary_error = None
with (destination / 'command.log').open('w') as stream:
    process = commands.start('benchmark', args, stdout=stream, stderr=subprocess.STDOUT)
    try:
        process.wait()
    except Exception as error:
        primary_error = error
    finally:
        try:
            if 'benchmark' in commands.active:
                commands.release('benchmark', grace=3)
        except Exception as error:
            if primary_error is None:
                primary_error = error
rc = process.returncode
try:
    events, available = archive_available(bep, destination, target, began_ms)
except Exception as error:
    if primary_error is None:
        primary_error = error
if primary_error is not None:
    (destination / 'primary-failure.json').write_text(json.dumps({'type': type(primary_error).__name__, 'message': str(primary_error), 'exit': rc}, indent=2) + '\n')
    raise primary_error
(destination / 'command-result.json').write_text(json.dumps({'exit': rc, 'wallSeconds': time.monotonic() - began,
                                                           'source': os.environ['SOURCE'], 'runtime': runtime,
                                                           'notPerformanceSampling': True}, indent=2) + '\n')
assert not command(['sudo', 'docker', 'ps', '-aq'], timeout=5).split()
assert command(['sudo', 'docker', 'image', 'inspect', '--format', '{{.Id}}', 'gvisor.dev/images/basic/alpine:latest'], timeout=5).strip() == image
assert available['xmlCopied'] and not available['errors'], available
attempts = [event for event in events if 'testResult' in event]
summaries = [event for event in events if 'testSummary' in event]
finished = [event['finished'] for event in events if 'finished' in event]
assert len(attempts) == len(summaries) == 1
attempt, summary = attempts[0], summaries[0]
assert attempt['id']['testResult']['label'] == summary['id']['testSummary']['label'] == target
result = attempt['testResult']
assert len([item for item in result.get('testActionOutput', []) if item['name'] == 'test.xml']) == 1
# The failure-evidence path already archives the exact fresh BEP-owned XML.
# Validate that immutable copy; Bazel's read-only mode is preserved by copy2.
xml = destination / 'test.xml'
assert xml.is_file() and not xml.is_symlink()
assert rc == 0 and result['status'] == 'PASSED'
assert result.get('cachedLocally', False) is False and result.get('cachedRemotely', False) is False
assert result.get('executionInfo', {}).get('cachedRemotely', False) is False
require_fresh(int(result['testAttemptStartMillisEpoch']), began_ms)
require_fresh(xml.stat().st_mtime_ns // 1_000_000, began_ms)
assert summary['testSummary']['overallStatus'] == 'PASSED'
assert summary['testSummary']['totalRunCount'] == 1 and summary['testSummary'].get('totalNumCached', 0) == 0
assert len(finished) == 1 and finished[0].get('overallSuccess') is True
root = ET.parse(xml).getroot()
cases = list(root.iter('testcase'))
assert len(cases) == 1 and cases[0].get('name') == 'TestFuseSmoke'
assert all(cases[0].find(kind) is None for kind in ('failure', 'error', 'skipped'))
for suite in root.iter('testsuite'):
    assert all(int(suite.get(kind, '0')) == 0 for kind in ('failures', 'errors', 'skipped'))
(destination / 'verified.json').write_text(json.dumps({'passed': True, 'actualCases': 1, 'freshUncachedBEPAndXML': True,
                                                     'source': os.environ['SOURCE'], 'runtime': runtime,
                                                     'runtimeSelection': 'Explicit RUNTIME through the unchanged canonical harness; configured Docker path/args and actual runtime preflight verified separately',
                                                     'directContainerIdentityObserved': False}, indent=2) + '\n')

complete(results, 'owner-' + label)
