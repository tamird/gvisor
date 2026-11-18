"""Require the task's exact runtime paths before any ordinary-gofer pilot."""

import json
from pathlib import Path
import subprocess
import sys
import time


output = Path(sys.argv[1])
configuration_path = Path(sys.argv[2])
expected = dict(value.split('=', 1) for value in sys.argv[3:])
if set(expected) != {'vfio-gofer-pilot'}:
    raise ValueError('Expected exactly the task VFIO gofer pilot runtime')
configuration = json.loads(configuration_path.read_text())
configured = {name: configuration.get('runtimes', {}).get(name) for name in expected}
report = {'expectedPaths': expected, 'configuredTaskRuntimes': configured, 'observations': []}
output.write_text(json.dumps(report, indent=2) + '\n')
if any(not isinstance(configured[name], dict) or configured[name].get('path') != path
       for name, path in expected.items()):
    raise SystemExit('Task runtime configuration paths do not match the installed binaries')
deadline = time.monotonic() + 60
while True:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SystemExit('Docker did not activate the exact task runtime within sixty seconds')
    try:
        result = subprocess.run(['docker', 'info', '--format', '{{json .Runtimes}}'],
                                text=True, capture_output=True, timeout=min(10, remaining), check=True)
        runtimes = json.loads(result.stdout)
        actual = {name: runtimes.get(name) for name in expected}
        report['observations'].append({'runtimeNames': sorted(runtimes), 'taskRuntimes': actual})
        ready = all(isinstance(actual[name], dict) and actual[name].get('path') == path
                    for name, path in expected.items())
    except (subprocess.SubprocessError, json.JSONDecodeError) as error:
        report['observations'].append({'error': str(error)})
        ready = False
    report['ready'] = ready
    output.write_text(json.dumps(report, indent=2) + '\n')
    if ready:
        break
    time.sleep(min(1, max(0, deadline - time.monotonic())))
