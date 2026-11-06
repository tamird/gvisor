"""Compare the TCP Nogo test log on master and the fork revision."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.parse

BASE = '1f49f256eae8d2dd2c69ea4cfe9ef2a5bb05516d'
PRODUCT = '3194009ff903283d5b66b4b8fcd2cd490d1c3cf1'
TARGET = '//pkg/tcpip/transport/tcp:tcp_nogo'
ROOT = Path(os.environ['RUNNER_TEMP']) / 'handshake-locks'
FLAGS = [
    '--config=x86_64', '--lockfile_mode=off', '--jobs=4', '--local_resources=memory=8192',
    '--strategy=GoStdlib=processwrapper-sandbox', '--strategy=GoStdlibList=processwrapper-sandbox',
    '--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox',
    '--remote_executor=', '--remote_cache=', '--bes_backend=', '--spawn_strategy=linux-sandbox',
    '--strategy=TestRunner=linux-sandbox', '--sandbox_default_allow_network=false',
    '--nocache_test_results', '--test_output=errors', '--color=no', '--curses=no', '--noshow_progress',
]


def check(label, commit):
    subprocess.run(['git', 'checkout', '-q', '--detach', commit], check=True)
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == commit
    assert not subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'])
    events = ROOT / (label + '.bep')
    output = ROOT / (label + '.bazel')
    with output.open('wb') as log:
        result = subprocess.run([str(ROOT/'bazelisk'), '--nosystem_rc', '--nohome_rc',
                                 'test', *FLAGS, '--build_event_json_file='+str(events), TARGET],
                                stdout=log, stderr=subprocess.STDOUT)
    record = {'source': commit, 'exit': result.returncode, 'targets': []}
    if events.exists():
        for line in events.open():
            event = json.loads(line)
            if event['id'].get('testResult', {}).get('label') != TARGET:
                continue
            test = event['testResult']
            record['targets'].append({'status': test.get('status'), 'strategy': test.get('executionInfo', {}).get('strategy'),
                                      'cached': test.get('cachedLocally', False)})
            log, = [item for item in test['testActionOutput'] if item['name'] == 'test.log']
            parts = urllib.parse.urlsplit(log['uri'])
            assert parts.scheme == 'file'
            data = Path(urllib.parse.unquote(parts.path)).read_bytes()
            record['test_log_sha256'] = hashlib.sha256(data).hexdigest()
            record['test_log_bytes'] = len(data)
            record['test_log'] = (data if len(data) <= 32000 else data[:16000]+b'\n...\n'+data[-16000:]).decode(errors='replace')
    if not record['targets']:
        record['bazel_end'] = output.read_bytes()[-18000:].decode(errors='replace')
    print(label, record['exit'], record['targets'], flush=True)
    return record


def main():
    receipts = {}
    for label, commit in [('master', BASE), ('candidate', PRODUCT)]:
        receipts[label] = check(label, commit)
        (ROOT / 'receipt.json').write_text(json.dumps(receipts, indent=2)+'\n')
    assert all(record['targets'] and 'test_log' in record for record in receipts.values())
    assert receipts['candidate']['exit'] == 0


if __name__ == '__main__':
    main()
