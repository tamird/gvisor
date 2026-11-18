"""Bind qualified source blobs and require the exact tools-owner XML union."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

mode = sys.argv[1]
expected = json.loads(Path(sys.argv[2]).read_text())
results = Path(sys.argv[3])
if mode == 'bind':
    source = sys.argv[4]
    assert source == expected['source']
    actual = {path: subprocess.check_output(['git', 'rev-parse', source + ':' + path], text=True).strip() for path in expected['qualifiedBlobs']}
    assert actual == expected['qualifiedBlobs'], actual
    (results / 'qualified-source-blobs.json').write_text(json.dumps({'source': source, 'qualifiedBlobs': actual, 'allMatch': True}, indent=2) + '\n')
    raise SystemExit(0)
assert mode == 'xml'
logs = Path(sys.argv[4]) / 'test/benchmarks/tools/tools_test'
destination = results / 'tools-owner'
destination.mkdir()
for name in ('test.log', 'test.xml'):
    shutil.copy2(logs / name, destination / name)
cases = list(ET.parse(destination / 'test.xml').getroot().iter('testcase'))
names = [case.get('name') for case in cases]
failed = [case.get('name') for case in cases if case.find('failure') is not None]
errors = [case.get('name') for case in cases if case.find('error') is not None]
skipped = [case.get('name') for case in cases if case.find('skipped') is not None]
proof = {'source': expected['source'], 'cases': len(cases), 'names': sorted(names), 'failed': failed, 'errors': errors, 'skipped': skipped, 'exactExpectedUnion': sorted(names) == expected['expectedToolsCases']}
(destination / 'proof.json').write_text(json.dumps(proof, indent=2) + '\n')
assert proof['exactExpectedUnion'] and not failed and not errors and not skipped, proof
