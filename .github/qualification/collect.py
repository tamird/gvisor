"""Capture one completed test phase and fail closed on missing case evidence."""

import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

TIMESTAMP_CASES = {
    'TestTimeStampEnabledConnect', 'TestTimeStampPAWSExpiry',
    'TestTimeStampZeroWindowProbe', 'TestTimeStampDisabledConnect',
    'TestTimeStampEnabledAccept', 'TestTimeStampDisabledAccept',
    'TestSendGreaterThanMTUWithOptions', 'TestSegmentNotDroppedWhenTimestampMissing',
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
        xml = captured / 'test.xml'
        if not xml.is_file():
            problems.append(f'Missing actual test XML: {target}')
            continue
        root = ET.parse(xml).getroot()
        cases = list(root.iter('testcase'))
        if not cases:
            problems.append(f'Empty actual test XML: {target}')
        for suite in root.iter('testsuite'):
            for attribute in ('failures', 'errors', 'skipped'):
                if int(suite.get(attribute, '0')) != 0:
                    problems.append(f'Nonzero suite {attribute}: {target}')
        names = {case.get('name', '').split('/')[0] for case in cases}
        required = set()
        if target.endswith(':tcp_timestamp_test'):
            required = TIMESTAMP_CASES
        elif 'tcp_paws_mechanism_' in target:
            required = {'TestPAWSMechanism'}
        missing = required - names
        if missing:
            problems.append(f'Missing required cases for {target}: {sorted(missing)}')
        failures = [case.get('name') for case in cases
                    if any(case.find(tag) is not None for tag in ('skipped', 'error', 'failure'))
                    or case.get('status') in ('notrun', 'skipped')]
        if failures:
            problems.append(f'Skipped or failed cases for {target}: {failures}')
        reports.append({'target': target, 'testcases': len(cases), 'required': sorted(required),
                        'names': sorted(names), 'skippedOrFailed': failures})
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'accounting.json').write_text(json.dumps({
        'targets': reports, 'problems': problems, 'status': 'failed' if problems else 'passed',
    }, indent=2) + '\n')
    if problems:
        raise SystemExit('\n'.join(problems))


if __name__ == '__main__':
    main()
