import json
from pathlib import Path
import re
import shutil
import sys
import xml.etree.ElementTree as ET


logs, output, manifest_path = map(Path, sys.argv[1:4])
phase = sys.argv[4]
manifest = json.loads(manifest_path.read_text())
owners = {'tcp': 'pkg/tcpip/transport/tcp/tcp_test'}
if phase == 'native':
    owners.update(e2e='pkg/tcpip/transport/tcp/test/e2e/tcp_test',
                  nogo='pkg/tcpip/transport/tcp/tcp_nogo')
report = {'source': manifest['source'], 'phase': phase,
          'overlay': [] if phase == 'native' else ['pkg/tcpip/transport/tcp/endpoint_test.go'],
          'owners': {}}
for owner, relative in owners.items():
    destination = output / owner
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('test.xml', 'test.log'):
        shutil.copy2(logs / relative / name, destination / name)
    cases = ET.parse(destination / 'test.xml').getroot().findall('.//testcase')
    failures = [case.attrib for case in cases if any(case.find(tag) is not None for tag in ('failure', 'error'))]
    skips = [dict(case.attrib, reason=case.find('skipped').text if case.find('skipped') is not None else '')
             for case in cases if case.find('skipped') is not None or case.get('status') == 'notrun' or case.get('result') == 'suppressed']
    record = {'cases': len(cases), 'failures': failures, 'skips': skips}
    report['owners'][owner] = record
    (output / 'case-report.json').write_text(json.dumps(report, indent=2) + '\n')
    assert cases and not failures and not skips, (owner, record)
    if owner == 'tcp':
        names = {case.get('name', '').split('/', 1)[0].rsplit('.', 1)[-1] for case in cases}
        required = set(manifest['fairness'])
        if phase in ('native', 'race'):
            required.update(manifest['tcpOwnerTopLevelTests'])
        if phase != 'native' and phase != 'onecpu':
            required.add(manifest['supplemental'])
        assert required <= names, (phase, sorted(required - names))
        text = (destination / 'test.log').read_text()
        assert all(re.search(r'^--- PASS: ' + re.escape(name) + r' \(', text, re.M) for name in required), phase
        record['requiredPassed'] = sorted(required)
    elif owner == 'nogo':
        text = (destination / 'test.log').read_text()
        actual = re.findall(r'^(amd64|arm64)\.\.\.(PASS|FAIL)$', text, re.M)
        record['analyzers'] = actual
        assert sorted(actual) == [('amd64', 'PASS'), ('arm64', 'PASS')], actual
(output / 'case-report.json').write_text(json.dumps(report, indent=2) + '\n')
