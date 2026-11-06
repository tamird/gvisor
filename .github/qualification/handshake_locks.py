"""Retain named handshake outcomes and the two intentional lock diagnostics."""
import json
import os
from pathlib import Path
import re
import subprocess

PRODUCT = '9852d233e0ec8126d3316f62a62e2c7ce66d51f5'
ROOT = Path(os.environ['RUNNER_TEMP']) / 'handshake-locks'
NOGO = '//pkg/tcpip/transport/tcp:tcp_nogo'
CASES = {
    '//pkg/tcpip/transport/tcp/test/e2e:forwarder_test': ('TestForwarderSendMSSLessThanMTU', 'TestForwarderDoesNotRejectECNFlags', 'TestForwarderFailedConnect', 'TestForwarderDroppedStats'),
    '//pkg/tcpip/transport/tcp/test/e2e:tcp_test': ('TestAcceptedInheritsDelayOption', 'TestTCPDeferAccept', 'TestTCPDeferAcceptTimeout', 'TestListenSynRcvdQueueFull'),
}
COMMON = [
    '--config=x86_64', '--lockfile_mode=off', '--jobs=4', '--local_resources=memory=8192',
    '--strategy=GoStdlib=processwrapper-sandbox', '--strategy=GoStdlibList=processwrapper-sandbox',
    '--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox',
    '--remote_executor=', '--remote_cache=', '--bes_backend=', '--spawn_strategy=linux-sandbox',
    '--strategy=TestRunner=linux-sandbox', '--sandbox_default_allow_network=false',
    '--nocache_test_results', '--test_env=GO_TEST_WRAP_TESTV=1', '--test_output=all',
    '--color=no', '--curses=no', '--noshow_progress',
]
OUT = ROOT / 'receipt.json'


def save(report):
    OUT.write_text(json.dumps(report, indent=2) + '\n')


def run(name, targets, flags=()):
    events = ROOT / (name + '.bep')
    log = ROOT / (name + '.log')
    cmd = [str(ROOT/'bazelisk'), '--nosystem_rc', '--nohome_rc', 'test', *COMMON, *flags, '--build_event_json_file='+str(events), *targets]
    with log.open('wb') as out:
        code = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT).returncode
    results = []
    if events.exists():
        with events.open() as stream:
            for line in stream:
                event = json.loads(line)
                target = event['id'].get('testResult', {}).get('label')
                if target in targets:
                    result = event['testResult']
                    results.append([target, result.get('status'), result.get('executionInfo', {}).get('strategy'), result.get('cachedLocally', False)])
    text = log.read_text(errors='replace')
    diag = [s.strip()[:480] for s in text.splitlines() if re.search(r'(accept|forwarder|dispatcher)\.go:\d+|Missing lock|return.*lock|inconsistent lock|ERROR:', s, re.I)]
    print(name, code, results, diag[:16], flush=True)
    return {'exit': code, 'targets': results, 'diagnostics': diag[:45]}, text


def main():
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    assert head == PRODUCT and not subprocess.check_output(['git', 'status', '--porcelain'])
    report = {'source': head}
    save(report)
    report['positive'], _ = run('positive', [NOGO])
    save(report)
    pat = '^(' + '|'.join(x for cases in CASES.values() for x in cases) + ')$'
    report['packets'], output = run('packets', list(CASES), ['--test_filter='+pat,'--test_arg=-test.v'])
    report['passed_cases'] = sorted(set(re.findall(r'--- PASS: (Test[A-Za-z0-9_]+)(?=\s)',output)))
    save(report)
    path = Path('pkg/tcpip/transport/tcp/accept.go')
    old = path.read_text()
    call = '\te.propagateInheritableOptionsLocked(ep)'
    assert old.count(call) == 1
    injection = '\te.mu.Unlock()\n\tep.mu.Unlock()\n'+call+'\n\tep.mu.Lock()\n\te.mu.Lock()'
    changed = old.replace(call, injection)
    line = changed[:changed.index(call)].count('\n')+1
    path.write_text(changed)
    try:
        report['negative'], output = run('negative',[NOGO])
    finally:
        path.write_text(old)
    report['negative_line'] = line
    report['negative_at_call'] = [x for x in report['negative']['diagnostics'] if f'accept.go:{line}:' in x]
    save(report)
    assert report['positive']['exit'] == report['packets']['exit'] == 0
    assert all(x[1]=='PASSED' and x[3] is False for lane in ('positive','packets') for x in report[lane]['targets'])
    assert len(report['positive']['targets'])==1 and len(report['packets']['targets'])==2
    assert set(report['passed_cases']) == {x for cases in CASES.values() for x in cases}
    assert report['negative']['exit'] != 0
    for lock in ('e.mu', 'n.mu'):
        assert any(f'must hold {lock} ' in item and 'propagateInheritableOptionsLocked' in item
                   for item in report['negative_at_call'])


if __name__=='__main__':
    main()
