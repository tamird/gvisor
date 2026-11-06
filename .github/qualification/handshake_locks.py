"""Keep original Nogo and per-shard handshake test outcomes."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse

PRODUCT = 'abc461928f2488ac0aaf1a3ec046a3ee0b200c79'
NOGO = '//pkg/tcpip/transport/tcp:tcp_nogo'
ROOT = Path(os.environ['RUNNER_TEMP']) / 'handshake-locks'
CASES = {
    '//pkg/tcpip/transport/tcp/test/e2e:forwarder_test': ('TestForwarderSendMSSLessThanMTU', 'TestForwarderDoesNotRejectECNFlags', 'TestForwarderFailedConnect', 'TestForwarderDroppedStats'),
    '//pkg/tcpip/transport/tcp/test/e2e:tcp_test': ('TestAcceptedInheritsDelayOption', 'TestTCPDeferAccept', 'TestTCPDeferAcceptTimeout', 'TestListenSynRcvdQueueFull', 'TestSynCookiePassiveSendMSSLessThanMTU'),
}
FLAGS = [
    '--config=x86_64', '--lockfile_mode=off', '--jobs=4', '--local_resources=memory=8192',
    '--strategy=GoStdlib=processwrapper-sandbox', '--strategy=GoStdlibList=processwrapper-sandbox',
    '--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox',
    '--remote_executor=', '--remote_cache=', '--bes_backend=', '--spawn_strategy=linux-sandbox',
    '--strategy=TestRunner=linux-sandbox', '--sandbox_default_allow_network=false',
    '--nocache_test_results', '--test_output=errors', '--color=no', '--curses=no', '--noshow_progress',
]


def run(name, targets, more=()):
    events = ROOT / (name+'.events')
    output = ROOT / (name+'.out')
    with output.open('wb') as log:
        cmd = [str(ROOT/'bazelisk'), '--nosystem_rc', '--nohome_rc', 'test', *FLAGS, *more,
               '--build_event_json_file='+str(events), *targets]
        code = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
    results = []
    if events.exists():
        for line in events.open():
            event = json.loads(line)
            identity = event['id'].get('testResult', {})
            if identity.get('label') not in targets:
                continue
            result = event['testResult']
            item = {'label': identity['label'], 'shard': identity.get('shard',0), 'status': result.get('status'),
                    'strategy': result.get('executionInfo', {}).get('strategy'), 'cached': result.get('cachedLocally',False)}
            results.append(item)
            entry, = [x for x in result['testActionOutput'] if x['name']=='test.log']
            loc=urllib.parse.urlsplit(entry['uri'])
            assert loc.scheme == 'file'
            data = Path(urllib.parse.unquote(loc.path)).read_bytes()
            text = data.decode(errors='replace')
            item['log_sha256']=hashlib.sha256(data).hexdigest()
            if name=='nogo' or item['status']!='PASSED':
                item['log']=(text if len(text)<9000 else text[:4500]+'\n...\n'+text[-4500:])
            if name=='sockets':
                item['passed_cases']=sorted(set(re.findall(r'--- PASS: (Test[A-Za-z0-9_]+)(?=\s)',text)))
    answer = {'exit':code, 'results':results}
    if not results:
        answer['bazel_end']=output.read_bytes()[-18000:].decode(errors='replace')
    print(name, code, [(x['label'],x['shard'],x['status']) for x in results], flush=True)
    return answer


def main():
    assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()==PRODUCT
    assert not subprocess.check_output(['git','status','--porcelain'])
    p=ROOT/'receipt.json'
    report={'source':PRODUCT}
    p.write_text(json.dumps(report)+'\n')
    report['nogo']=run('nogo',[NOGO])
    p.write_text(json.dumps(report,indent=2)+'\n')
    expression='^('+'|'.join(case for cases in CASES.values() for case in cases)+')$'
    report['sockets']=run('sockets',list(CASES),['--test_filter='+expression,'--test_env=GO_TEST_WRAP_TESTV=1','--test_arg=-test.v'])
    p.write_text(json.dumps(report,indent=2)+'\n')
    assert all(report[lane]['exit']==0 for lane in ('nogo','sockets'))
    owners=report['nogo']['results']
    assert len(owners)==1 and 'amd64...PASS' in owners[0]['log'] and 'arm64...PASS' in owners[0]['log']
    rows=report['sockets']['results']
    assert all(x['status']=='PASSED' and not x['cached'] for x in owners+rows)
    for target, required in CASES.items():
        passed={c for row in rows if row['label']==target for c in row['passed_cases']}
        assert passed==set(required), (target,passed)

    # Return an unlocked child after it has been initialized. The diagnostic
    # must be on the constructor's return, where it transfers the lock.
    source = Path('pkg/tcpip/transport/tcp/accept.go')
    text = source.read_text()
    tail = '\tn.rcvQueueMu.Unlock()\n\n\treturn n'
    assert text.count(tail) == 1
    before, _, after = text.partition(tail)
    unlocked = '\tn.rcvQueueMu.Unlock()\n\n\tn.mu.Unlock()\n\treturn n'
    line = before.count('\n') + unlocked.count('\n') + 1
    source.write_text(before + unlocked + after)
    negative = run('unlocked_child', [NOGO])
    report['unlocked_child'] = negative
    report['unlocked_return_line'] = line
    p.write_text(json.dumps(report, indent=2) + '\n')
    assert negative['exit'] != 0
    output = '\n'.join(row.get('log', '') for row in negative['results']) + negative.get('bazel_end', '')
    assert any(f'accept.go:{line}:' in finding and 'not held exclusively' in finding
               for finding in output.splitlines()), (line, output[-2000:])


if __name__=='__main__':
    main()
