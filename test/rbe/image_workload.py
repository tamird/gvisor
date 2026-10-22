"""Qualify the requested port-range setup and caller-scope corrections."""
import json
import os
from pathlib import Path
import shlex

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ['APPLICABILITY_QUALIFICATION_CONFIG'])
    targets = config['targets']
    options = ['--config=x86_64', '--compilation_mode=opt', '--lockfile_mode=error',
               '--noannounce_rc', '--remote_download_outputs=all',
               '--build_event_json_file_path_conversion=false']
    record = {'status': 'INCOMPLETE', 'source': config['source'], 'variants': [], 'tests': []}
    errors = []
    try:
        info = invoke('execution-root', ['info', *options, 'execution_root'], 'command', targets=[])
        if info['status'] != 'PASS':
            raise RuntimeError('Canonical execution root unavailable')
        root = Path((evidence / 'invocation-execution-root/stdout.log').read_text().strip())
        if not root.is_absolute() or not root.is_dir():
            raise ValueError('Invalid execution root')
        log = private / 'port-range-build-execution.json'
        built = invoke('port-range-build', ['build', *options, '--keep_going',
                       '--execution_log_json_file=' + str(log), '--noexecution_log_sort', *targets],
                       'build', targets=targets)
        if log.is_file():
            record_execution(log, 'x86_64', 'port-range-build')
        if built['status'] != 'PASS':
            raise RuntimeError('Port range fixture build failed')
        owners = {(report.Owner(owner['label'], owner['configuration']), 'default') for owner in built['expected']}
        outputs = report.advertised_build_outputs(evidence / 'invocation-port-range-build/events.jsonl', owners)
        paths = {}
        for output in outputs:
            name = Path(output['name'])
            if name.is_absolute() or '..' in name.parts or name.parts[:1] != ('bazel-out',):
                raise ValueError('Invalid configured output path')
            path = root / name
            actual = report.file_record(path)
            if any(actual[key] != output[key] for key in ('bytes', 'sha256')):
                raise ValueError('Materialized output differs from default BEP')
            label = output['identity']['label']
            if label in paths:
                raise ValueError('Expected one output per selected owner')
            paths[label] = path
        if set(paths) != set(targets):
            raise ValueError('Missing configured output')
        record['outputs'] = outputs
        for variant in config['variants']:
            script = paths[variant['label']].read_text()
            arguments = shlex.split(next(line for line in script.splitlines() if line.startswith('exec ')))
            limited = '--limit-ephemeral-ports' in arguments
            containers = [arg for arg in arguments if arg.startswith('--container=')]
            if limited != variant['limited'] or containers != ['--container=' + str(variant['limited'])]:
                raise ValueError(f'Unexpected configured wrapper arguments: {arguments}')
            record['variants'].append({'label': variant['label'], 'limited': limited, 'arguments': arguments})
            (evidence / (variant['name'] + '-configured-wrapper.sh')).write_text(script)
        # Use the existing owning cases; native precedes both sandbox variants.
        for variant in config['variants']:
            result = invoke('port-range-' + variant['name'], ['test', *options,
                            '--nocache_test_results', '--test_sharding_strategy=disabled',
                            '--test_env=GTEST_FILTER=' + ':'.join(config['cases']), variant['label']],
                            'test', targets=[variant['label']])
            record['tests'].append({'label': variant['label'], 'status': result['status'],
                                    'cases': result.get('case_counts'), 'skips': result.get('skips')})
            if result['status'] != 'PASS' or result.get('case_counts') != {'passed': 2, 'failed': 0, 'skipped': 0}:
                errors.append(variant['name'])
        permission_output = evidence / 'permission-denied' / 'test.xml'
        result = invoke('permission-denied', ['run', '--lockfile_mode=error', '--noannounce_rc',
                        '@rules_python//python/bin:python', '--',
                        str(Path.cwd() / 'test/rbe/port_range_permission.py'),
                        str(paths[config['setup']]), str(paths[config['binary']]), str(permission_output)],
                        'command', targets=[])
        record['permission_denied_status'] = result['status']
        if result['status'] != 'PASS':
            errors.append('permission-denied')
        if errors:
            raise RuntimeError('Port range validation failed: ' + ', '.join(errors))
        record['status'] = 'PASS'
    finally:
        record['failed_groups'] = errors
        report.write_json(evidence / 'port-range-feedback.json', record)
