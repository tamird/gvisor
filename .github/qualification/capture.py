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
    actual = {path: subprocess.check_output(['git', 'rev-parse', source + ':' + path], text=True).strip() for path in expected['sourceBlobs']}
    assert actual == expected['sourceBlobs'], actual
    (results / 'source-blobs.json').write_text(json.dumps({'source': source, 'sourceBlobs': actual, 'allMatch': True}, indent=2) + '\n')
    raise SystemExit(0)
if mode == 'native':
    import math
    import re
    for phase, wanted in expected['nativeLeaves'].items():
        text = (results / (phase + '.log')).read_text().replace('\x16', '')
        rows = {}
        for line in text.splitlines():
            match = re.match(r'^(Benchmark\S+)-4\s+(\d+)\s+(.*)$', line)
            if not match:
                continue
            name, count, rest = match.groups()
            fields = rest.split()
            assert name not in rows and count == '1' and len(fields) % 2 == 0
            metrics = {fields[index+1]: float(fields[index]) for index in range(0, len(fields), 2)}
            assert all(math.isfinite(value) and value >= 0 for value in metrics.values())
            required = ('MB/s', 'bandwidth.bytes_per_second') if phase == 'native-iperf' else ('requests_per_second.QPS', 'average_latency.s')
            assert all(key in metrics and metrics[key] > 0 for key in required), metrics
            rows[name] = metrics
        assert set(rows) == set(wanted), rows
        (results / (phase + '-proof.json')).write_text(json.dumps({'source': expected['source'], 'functionalN1Only': True, 'rows': rows}, indent=2) + '\n')
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
assert all(case.get('status', 'run') == 'run' and case.get('result', 'completed') == 'completed' for case in cases)
