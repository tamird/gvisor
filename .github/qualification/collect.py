"""Capture one completed test phase and fail closed on missing case evidence."""

import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REQUIRED = {
    '//runsc/gofer/fs:lisafs_test': {'TestFSGofer'},
    '//runsc/gofer/fs:groups_test': {
        'TestGroupsAlreadySet', 'TestSupportedMessagesNeedSetgroups',
        'TestConnectWithGroupsKeepsOurGroups', 'TestConnectWithGroupsEndsItsThread',
        'TestConnectWithGroupsPassesPanicOn', 'TestConnectWithGroupsFailureSkipsConnect',
    },
    '//test/runner:runner_test': {'TestPrintCheckpointDir', 'TestCollateXMLs',
        'TestCollateXMLsNoReports', 'TestCollateXMLsMalformedReport'},
}


def main() -> None:
    destination = Path(sys.argv[1])
    targets = sys.argv[2:]
    reports = []
    problems = []
    for target in targets:
        relative = target.removeprefix('//').replace(':', '/')
        source = Path('bazel-testlogs', relative)
        captured = destination / relative
        if not source.is_dir():
            problems.append(f'Missing test output tree: {target}')
            continue
        shutil.copytree(source, captured, symlinks=False)
        direct = captured / 'test.xml'
        xmls = [direct] if direct.is_file() else sorted(captured.glob('shard_*_of_*/test.xml'))
        if not xmls:
            problems.append(f'Missing actual test XML: {target}')
            continue
        if not direct.is_file():
            parts = [re.fullmatch(r'shard_(\d+)_of_(\d+)', xml.parent.name) for xml in xmls]
            if not all(parts):
                problems.append(f'Invalid shard names: {target}')
                continue
            totals = {int(part[2]) for part in parts}
            if len(totals) != 1 or {int(part[1]) for part in parts} != set(range(1, next(iter(totals)) + 1)):
                problems.append(f'Incomplete actual XML shards: {target}')
        cases = []
        identities = set()
        for xml in xmls:
            root = ET.parse(xml).getroot()
            actual = list(root.iter('testcase'))
            if not actual:
                problems.append(f'Empty actual test XML: {target}: {xml.parent.name}')
            for case in actual:
                identity = (case.get('classname'), case.get('name'))
                if identity in identities:
                    problems.append(f'Duplicate actual testcase: {target}: {identity}')
                identities.add(identity)
            cases.extend(actual)
            for suite in root.iter('testsuite'):
                for attribute in ('failures', 'errors', 'skipped'):
                    if int(suite.get(attribute, '0')) != 0:
                        problems.append(f'Nonzero suite {attribute}: {target}')
        names = {case.get('name', '').split('/')[0] for case in cases}
        required = REQUIRED[target]
        missing = required - names
        if missing:
            problems.append(f'Missing required cases for {target}: {sorted(missing)}')
        failures = [case.get('name') for case in cases
                    if any(case.find(tag) is not None for tag in ('skipped', 'error', 'failure'))
                    or case.get('status') in ('notrun', 'skipped')]
        if failures:
            problems.append(f'Skipped or failed cases for {target}: {failures}')
        reports.append({'target': target, 'testcases': len(cases), 'required': sorted(required),
                        'names': sorted(names), 'xmlFiles': [str(xml.relative_to(captured)) for xml in xmls], 'skippedOrFailed': failures})
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'accounting.json').write_text(json.dumps({
        'targets': reports, 'problems': problems, 'status': 'failed' if problems else 'passed',
    }, indent=2) + '\n')
    if problems:
        raise SystemExit('\n'.join(problems))


if __name__ == '__main__':
    main()
