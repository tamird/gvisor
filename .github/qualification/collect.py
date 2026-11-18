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


EXPECTED_SKIPS = {
    '//runsc/gofer/fs:lisafs_test': {
        'TestFSGofer/passthrough/UDS', 'TestFSGofer/virtual/UDS',
    },
}


def expected_skip(target: str, case: ET.Element) -> bool:
    skipped = case.find('skipped')
    return (case.get('classname') == 'lisafs_test'
            and case.get('name') in EXPECTED_SKIPS.get(target, set())
            and skipped is not None
            and 'server does not support BindAt RPC' in ''.join(skipped.itertext())
            and case.find('error') is None and case.find('failure') is None)


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
                for attribute in ('failures', 'errors'):
                    if int(suite.get(attribute, '0')) != 0:
                        problems.append(f'Nonzero suite {attribute}: {target}')
                if int(suite.get('skipped', '0')) != len(list(suite.iter('skipped'))):
                    problems.append(f'Inconsistent suite skipped count: {target}')
        names = {case.get('name', '').split('/')[0] for case in cases}
        required = REQUIRED[target]
        missing = required - names
        if missing:
            problems.append(f'Missing required cases for {target}: {sorted(missing)}')
        failures = [case.get('name') for case in cases
                    if any(case.find(tag) is not None for tag in ('skipped', 'error', 'failure'))
                    or case.get('status') in ('notrun', 'skipped')]
        declared_skips = {case.get('name') for case in cases if expected_skip(target, case)}
        if declared_skips != EXPECTED_SKIPS.get(target, set()):
            problems.append(f'Missing exact source-documented skips: {target}: {declared_skips}')
        failures = [case.get('name') for case in cases if case.get('name') in failures and not expected_skip(target, case)]
        if failures:
            problems.append(f'Skipped or failed cases for {target}: {failures}')
        reports.append({'target': target, 'testcases': len(cases), 'required': sorted(required),
                        'names': sorted(names), 'xmlFiles': [str(xml.relative_to(captured)) for xml in xmls], 'skippedOrFailed': failures, 'sourceDocumentedSkips': sorted(declared_skips)})
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'accounting.json').write_text(json.dumps({
        'targets': reports, 'problems': problems, 'status': 'failed' if problems else 'passed',
    }, indent=2) + '\n')
    if problems:
        raise SystemExit('\n'.join(problems))


if __name__ == '__main__':
    main()
