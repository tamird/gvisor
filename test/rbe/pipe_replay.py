"""Run only the frozen-clock fixture and retain both original outcomes."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

temp = Path(os.environ['RUNNER_TEMP'])
out = temp / 'pipe-public'
out.mkdir(exist_ok=True)
carrier = os.environ['GITHUB_SHA']
fixture = 'pkg/tcpip/transport/tcp/test/e2e/tcp_rack_test.go'
target = '//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test'
name = 'TestRACKRetainedSACKWithMissingTail'

def git(*args):
    return subprocess.check_output(['git', *args])

def capture(arm, source):
    row = {'source': source, 'fixture_blob': git('rev-parse', f'{carrier}:{fixture}').decode().strip(),
           'started_ns': time.time_ns(), 'target': target, 'case': name}
    try:
        git('checkout', '--quiet', '--detach', '--force', source)
        git('restore', '--source', carrier, '--worktree', '--', fixture)
        assert git('rev-parse','HEAD').decode().strip() == source
        assert git('diff','--name-only','HEAD').decode().splitlines() == [fixture]
        assert git('hash-object',fixture).decode().strip() == row['fixture_blob']
        args = ['bazelisk','--bazelrc='+os.environ['FIFO_RC'],'test','--config=x86_64',
                '--strategy=TestRunner=local','--spawn_strategy=linux-sandbox','--jobs=4',
                '--test_env=GO_TEST_WRAP_TESTV=1','--test_env=GVISOR_TEST_CLOCK_SOURCE=calibrated',
                '--define=gvisor_test_clock_source=calibrated','--test_sharding_strategy=disabled',
                '--nocache_test_results','--test_output=all','--test_timeout=240',
                '--test_filter=^'+name+'$','--build_event_json_file='+str(temp/(arm+'-bep.jsonl')),target]
        row['argv'] = args
        log = out/(arm+'.log')
        with log.open('w') as f:
            row['exit'] = subprocess.run(args, stdout=f, stderr=subprocess.STDOUT).returncode
        contents = log.read_text()
        # The build event contains the uncached, actual TestRunner result.
        results = [event['testResult'] for line in (temp/(arm+'-bep.jsonl')).read_text().splitlines()
                   if (event := json.loads(line)).get('testResult')]
        row['test_results'] = [(r['status'], r.get('cachedLocally',False),
                                 r.get('executionInfo',{}).get('cachedRemotely',False)) for r in results]
        row['evidence'] = [line.strip() for line in contents.splitlines() if ' PIPE ' in line]
        row['acks'] = sum(bool(re.search(r' PIPE ack=',line)) for line in row['evidence'])
        row['case_passed'] = bool(re.search(r'--- PASS: '+name+r'\b',contents))
        row['complete'] = (row['exit']==0 and row['case_passed'] and row['acks']==3 and
                           len(results)==1 and row['test_results']==[('PASSED',False,False)] and
                           any('PIPE first_timeout' in line for line in row['evidence']))
        print(arm, row['exit'], row['complete'], *row['evidence'], sep='\n', flush=True)
        if not row['complete']:
            print('\n'.join(contents.splitlines()[-40:]), flush=True)
    except Exception as e:
        row['error'] = str(e)
    row['ended_ns'] = time.time_ns()
    return row

heads = {arm:git('rev-parse',f'{carrier}^{i}').decode().strip()
         for i,arm in enumerate(['combined','also_15432'],1)}
rows = {arm:capture(arm,source) for arm,source in heads.items()}
delta = git('diff',heads['combined'],heads['also_15432'])
(out/'production.patch').write_bytes(delta)
(out/'fixture.go').write_bytes(git('show',f'{carrier}:{fixture}'))
meta = {'carrier':carrier,'source_pr':'https://github.com/google/gvisor/pull/15432',
        'source_pr_head':'00aaa82b9','production_delta_sha256':hashlib.sha256(delta).hexdigest(),
        'rows':rows,'scenario':'manual-clock CUBIC; preserve originals 4-6 through RTO; app backlog; no further loss'}
(out/'verdict.json').write_text(json.dumps(meta,indent=2)+'\n')
raise SystemExit(0 if all(r.get('complete') for r in rows.values()) else 1)
