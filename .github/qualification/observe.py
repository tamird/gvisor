"""Record task container identities and read-only, perturbative subject evidence."""

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from subject_metadata import probe_files

work, results = map(Path, sys.argv[1:3])
label, runtime = sys.argv[3:5]
images = json.loads((results / 'diagnostic-images.json').read_text())
stop = work / (label + '-observer-stop')
report = {'label': label, 'runtime': runtime, 'perturbative': True,
          'containers': {}, 'samples': [], 'errors': [], 'complete': False}
output = results / (label + '-observer.json')


def command(args, timeout=3):
    start = time.monotonic()
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {'argv': args, 'exit': result.returncode, 'stdout': result.stdout,
                'stderr': result.stderr, 'seconds': time.monotonic() - start}
    except subprocess.TimeoutExpired:
        return {'argv': args, 'timeout': timeout, 'seconds': time.monotonic() - start}


def probe(identifier):
    # These are read-only observations, not HTTP requests or configuration changes.
    script = 'rc=0; for f in /tmp/error.log /tmp/access.log /proc/1/limits /proc/net/netstat /proc/net/snmp /proc/net/tcp /proc/net/tcp6; do printf "\\nGVISOR_FILE %s\\n" "$f"; cat "$f"; value=$?; printf "\\nGVISOR_STATUS %s %s\\n" "$f" "$value"; test "$value" = 0 || rc=1; done; exit $rc'
    files = command(['sudo', 'docker', 'exec', identifier, 'sh', '-c', script], 5)
    evidence = probe_files(files.get('stdout', ''))
    return {'container': identifier, 'subjectFiles': files, 'fileEvidence': evidence,
            'logs': command(['sudo', 'docker', 'logs', identifier], 3)}



futures = []
probe_counts = {}
probe_futures = {}
stats_future = None
start = time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    (work / (label + '-observer-ready')).write_text('ready\n')
    while not stop.exists() and time.monotonic() - start < 180:
        listed = command(['sudo', 'docker', 'ps', '-q', '--no-trunc'])
        if listed.get('exit') != 0:
            report['errors'].append(listed)
            break
        for identifier in listed['stdout'].split():
            if identifier in report['containers']:
                continue
            inspected = command(['sudo', 'docker', 'inspect', identifier])
            if inspected.get('exit') != 0:
                report['errors'].append(inspected)
                continue
            info = json.loads(inspected['stdout'])[0]
            if info['Image'] not in images.values():
                raise ValueError('Unexpected image on previously empty task Docker host')
            report['containers'][identifier] = {
                'firstObservedSeconds': time.monotonic() - start, 'imageID': info['Image'],
                'name': info['Name'], 'state': info['State'], 'config': info['Config'],
                'hostConfig': info['HostConfig'], 'networkSettings': info['NetworkSettings'],
                'stopTimeAvailableAtInspection': not info['State']['FinishedAt'].startswith('0001-')}
        identifiers = listed['stdout'].split()
        for identifier in identifiers:
            if identifier not in report['containers'] or report['containers'][identifier]['imageID'] != images['httpd']:
                continue
            previous = probe_futures.get(identifier)
            count = probe_counts.get(identifier, 0)
            age = time.monotonic() - start - report['containers'][identifier]['firstObservedSeconds']
            if count < 3 and age >= (0, 1, 12)[count] and (previous is None or previous.done()):
                previous = pool.submit(probe, identifier)
                probe_futures[identifier] = previous
                futures.append(previous)
                probe_counts[identifier] = count + 1
        if stats_future is not None and stats_future.done():
            report['samples'].append(stats_future.result())
            stats_future = None
        if identifiers and stats_future is None:
            stats_future = pool.submit(command, ['sudo', 'docker', 'stats', '--no-stream', '--format', '{{json .}}', *identifiers], 3)
        output.write_text(json.dumps(report, indent=2) + '\n')
        if output.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Diagnostic observer exceeded 64 MiB metadata cap')
        time.sleep(0.2)
    report['subjectProbes'] = [future.result() for future in futures]
    if stats_future is not None:
        report['samples'].append(stats_future.result())
report['stoppedByOwner'] = stop.exists()
report['seconds'] = time.monotonic() - start
report['maxSubjectProbesPerServer'] = 3
report['complete'] = report['stoppedByOwner']
report['missingDiscoveryEvidence'] = bool(report['errors'])
output.write_text(json.dumps(report, indent=2) + '\n')
if output.stat().st_size > 64 * 1024 * 1024:
    raise ValueError('Final diagnostic observer exceeded 64 MiB metadata cap')
