"""Capture actual Bazel XML and undeclared outputs from checkpoint tests."""
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

out = Path(sys.argv[1])
mode = sys.argv[2]
targets = sys.argv[3:]
assert mode in ('passed', 'base', 'candidate'), mode
reports, problems = [], []
for target in targets:
    relative = target.removeprefix('//').replace(':', '/')
    source = Path('bazel-testlogs') / relative
    captured = out / relative
    if not source.is_dir():
        problems.append(f'Missing actual output tree: {target}')
        continue
    shutil.copytree(source, captured, symlinks=False)
    xmls = sorted(captured.glob('run_*_of_3/test.xml'))
    if mode == 'passed':
        xmls = [captured / 'test.xml']
    if len(xmls) != (1 if mode == 'passed' else 3) or not all(p.is_file() for p in xmls):
        problems.append(f'Missing actual run XML for {target}: {[str(x) for x in xmls]}')
        continue
    actual_failure = False
    for xml in xmls:
        root = ET.parse(xml).getroot()
        cases = list(root.iter('testcase'))
        if not cases:
            problems.append(f'Empty XML: {xml}')
        names = [case.get('name', '') for case in cases]
        skips = [case.attrib for case in cases if case.find('skipped') is not None or case.get('status') in ('notrun', 'skipped') or case.get('result') == 'suppressed']
        failures = [case for case in cases if any(case.find(tag) is not None for tag in ('failure', 'error'))]
        if skips:
            problems.append(f'Skipped actual cases: {xml}: {skips}')
        if mode in ('base', 'candidate'):
            for direction in ('wire_to_graph_to_wire', 'graph_to_wire_to_graph'):
                required = 'TestCheckpointRestoreCrossEngineChained/systrap/' + direction
                if required not in names:
                    problems.append(f'Missing exact required case: {required}: {xml}')
        if mode == 'base':
            leaf = 'TestCheckpointRestoreCrossEngineChained/systrap/wire_to_graph_to_wire'
            expected = 'error printing state: unreachable'
            reproduced = any(case.get('name') == leaf and expected in ''.join(case.itertext()) for case in failures)
            if failures and not reproduced:
                problems.append(f'BASE run failed without intended dump failure: {xml}')
            allowed = {leaf, 'TestCheckpointRestoreCrossEngineChained/systrap', 'TestCheckpointRestoreCrossEngineChained'}
            for case in failures:
                name = case.get('name', '')
                text = ''.join(case.itertext()).strip()
                generic_parent = name != leaf and text in ('', 'FAIL', 'Failed')
                if name not in allowed or case.find('error') is not None or not (expected in text or generic_parent and reproduced):
                    problems.append(f'Unexpected BASE failure: {xml}: {name}: {text}')
            actual_failure = actual_failure or reproduced
        elif failures:
            problems.append(f'Failed actual cases: {xml}: {[case.get("name") for case in failures]}')
        for suite in root.iter('testsuite'):
            attributes = ('errors', 'skipped') if mode == 'base' else ('failures', 'errors', 'skipped')
            for attr in attributes:
                if int(suite.get(attr, '0')):
                    problems.append(f'Nonzero suite {attr}: {xml}')
        reports.append({'target': target, 'xml': str(xml.relative_to(out)), 'cases': len(cases), 'names': names, 'failures': [case.get('name') for case in failures], 'skips': skips})
    if mode == 'base' and not actual_failure:
        problems.append('Expected BASE unreachable dump error absent from actual XML; investigate saved logs before claiming reproduction.')
out.mkdir(parents=True, exist_ok=True)
(out / 'accounting.json').write_text(json.dumps({'mode': mode, 'reports': reports, 'problems': problems}, indent=2) + '\n')
if problems:
    raise SystemExit('\n'.join(problems))
