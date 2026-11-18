"""Require actual completed guest VFIO Open results for all three modes."""
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

results = Path(sys.argv[1])
root = Path('bazel-testlogs/test/rbe/tcg/vfio_permanent_test').resolve()
if not root.is_dir():
    raise SystemExit('Missing actual guest test logs')
shutil.copytree(root, results / 'guest-testlogs', symlinks=False)
guest = root / 'test.outputs/guest'
outputs = guest / 'outputs'
if (guest / 'exit_status').read_text().strip() != '0':
    raise SystemExit('Guest did not complete successfully')
verdict = (outputs / 'verdict.txt').read_text().splitlines()
expected = ['native: 0', 'runsc_systrap: 0', 'runsc_systrap_direct: 0']
if verdict != expected:
    raise SystemExit('Incomplete or failed native-first VFIO verdicts')
report = {}
for mode in ('native', 'runsc_systrap', 'runsc_systrap_direct'):
    xml = ET.parse(outputs / (mode + '.xml')).getroot()
    cases = list(xml.iter('testcase'))
    if len(cases) != 1:
        raise SystemExit('Expected exactly one actual VFIO Open case')
    case = cases[0]
    if case.get('name') != 'Open' or case.get('classname') != 'VfioPCIDeviceTest':
        raise SystemExit('Unexpected actual VFIO case identity')
    if any(list(case.iter(tag)) for tag in ('failure', 'error', 'skipped')):
        raise SystemExit('VFIO case failed or skipped')
    if case.get('status', 'run') != 'run' or case.get('result', 'completed') != 'completed':
        raise SystemExit('VFIO case did not execute')
    report[mode] = {'cases': 1, 'name': 'VfioPCIDeviceTest.Open', 'passed': True}
(results / 'verified-guest-results.json').write_text(json.dumps(report, indent=2) + '\n')
