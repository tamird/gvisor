"""Match canonical subjects and distinguish actual evidence from attempted probes."""

import re


def final_subjects(observed, images, runtime):
    clients = [(identifier, info) for identifier, info in observed['containers'].items()
               if info['imageID'] == images['hey'] and '-n' in info['config']['Cmd']
               and info['config']['Cmd'][info['config']['Cmd'].index('-n') + 1] == '1000'
               and '-c' in info['config']['Cmd'] and info['config']['Cmd'][info['config']['Cmd'].index('-c') + 1] == '1000'
               and info['config']['Cmd'][-1] == 'http://server:80/latin1024k.txt']
    servers = []
    if len(clients) == 1:
        linked = {value.split(':')[0].lstrip('/') for value in clients[0][1]['hostConfig'].get('Links', [])}
        servers = [identifier for identifier, info in observed['containers'].items()
                   if info['imageID'] == images['httpd'] and info['name'].lstrip('/') in linked
                   and info['hostConfig']['Runtime'] == runtime
                   and info['config']['Cmd'] == ['sh', '-c', 'mkdir -p /tmp/html; cp -r /local/* /tmp/html/.; apache2 -X']]
    return [identifier for identifier, _ in clients], servers


def dynamic_evidence(observed, servers):
    probes = [probe for probe in observed.get('subjectProbes', []) if probe['container'] in servers]
    files = [item for probe in probes for item in probe.get('fileEvidence', []) if item['exit'] == 0 and item['nonempty']]
    logs = any(probe['logs'].get('exit') == 0 and (probe['logs'].get('stdout', '').strip() or probe['logs'].get('stderr', '').strip()) for probe in probes)
    logs = bool(logs or any(item['path'] in ('/tmp/error.log', '/tmp/access.log') for item in files))
    resources = any(item['path'].startswith('/proc/') for item in files)
    return {'attemptedProbes': len(probes), 'successfulNonemptyFiles': files,
            'usableLogs': logs, 'usableResources': resources,
            'usableDynamicEvidence': bool(files) or logs,
            'missingLogEvidence': not logs, 'missingResourceEvidence': not resources}


def probe_files(stdout):
    return [{'path': match[1], 'exit': int(match[3]), 'nonempty': bool(match[2].strip())}
            for match in re.finditer(r"\nGVISOR_FILE ([^\n]+)\n([\s\S]*?)\nGVISOR_STATUS \1 ([0-9]+)\n", stdout)]


def request_accounting_complete(proof):
    if proof is None or not proof['records']:
        return False
    return all(sum(item['report']['statusCounts'].values()) + item['report']['errorCount'] == item['report']['requested'] for item in proof['records'])
