import hashlib,json,os,re,subprocess,sys
from pathlib import Path
from datetime import datetime,timezone
from zipfile import ZipFile
T=Path(os.environ['RUNNER_TEMP']); OUT=T/'replay-public'; carrier=os.environ['GITHUB_SHA']
heads=json.loads((T/'tcp-heads.json').read_text())
packet='test/packetimpact/tests/tcp_rack_test.go'
migration='pkg/tcpip/transport/tcp/test/e2e/tcp_migration_test.go'
results={};errors=[]
def now():return datetime.now(timezone.utc).isoformat()
def git(*args):return subprocess.check_output(['git',*args])
def restore(source,paths):git('restore','--source',source,'--worktree','--',*paths)
def arm(name,key,target,kind,case=None):
    src=heads[key];d=OUT/name;d.mkdir()
    row=results[name]={'source':src,'target':target,'case':case,'started':now()}
    try:
        git('checkout','--quiet','--detach',src)
        assert git('rev-parse','HEAD').decode().strip()==src
        diagnostic=kind in ('wire','positive')
        paths=[]
        if diagnostic:
            paths=[packet];restore(carrier,paths)
        if kind=='restore':
            paths=[migration];restore(carrier,paths)
        changed=git('diff','--name-only','HEAD').decode().splitlines()
        assert sorted(changed)==sorted(paths),(src,changed,paths)
        row['extra_test_files']=changed
        flags=['bazelisk','--bazelrc='+os.environ['FIFO_RC'],'test','--config=x86_64','--strategy=TestRunner=local','--spawn_strategy=linux-sandbox','--jobs=4','--test_env=GO_TEST_WRAP_TESTV=1','--test_env=GVISOR_TEST_CLOCK_SOURCE=calibrated','--test_env=XTABLES_LOCKFILE','--define=gvisor_test_clock_source=calibrated','--strip=never','--test_sharding_strategy=disabled','--nocache_test_results','--test_output=all','--test_timeout=600','--build_event_json_file='+str(d/'bep.jsonl'),'--build_event_json_file_path_conversion=false']
        if case:flags+=['--test_filter=^'+case+'$']
        raw=T/(name+'.log')
        with raw.open('wb') as output:code=subprocess.run([*flags,target],stdout=output,stderr=subprocess.STDOUT).returncode
        row['exit']=code;row['ended']=now()
        (d/'source').write_text(src+'\n');(d/'exit').write_text(str(code)+'\n')
        root=Path('bazel-testlogs')/target[2:].replace(':','/')
        events=[json.loads(l) for l in (d/'bep.jsonl').read_text().splitlines()]
        row['invocation']=next(e['started']['uuid'] for e in events if 'started' in e)
        rows=[e['testResult'] for e in events if 'testResult'in e];assert len(rows)==1,len(rows)
        result=rows[0]
        assert not result.get('cachedLocally') and not result['executionInfo'].get('cachedRemotely') and result['executionInfo']['strategy'] in ('local','standalone'),result['executionInfo']
        from case_records import case_records
        raw_log=(root/'test.log').read_bytes()
        (d/'test.log').write_bytes(raw_log)
        log=raw_log.decode().replace('\x16','')
        expected=[case]+([case+'/'+c for c in ('none','one_byte','whole_packet','sack_suffix','sack_whole_then_trim')] if kind=='wire' else [])
        checks=case_records(raw_log,expected)
        row['case_records']=checks; row['successful_cases']=len(checks['passed']); row['skipped_cases']=checks['skipped']
        assert result['status']=='PASSED' and code==0 and not checks['skipped'] and len(checks['passed'])==len(expected), (code,checks)
        subprocess.run([sys.executable,str(T/'wire.py'),name,str(root),str(d)],check=True)
        obs=json.loads((d/'result.json').read_text())
        assert obs['observed']
        row['observations']=obs['rows']
        row['verified']=True
    except Exception as err:
        row['verified']=False;row['error']=repr(err)[:1900];errors.append(name)
        raw=T/(name+'.log')
        if raw.exists():
            clean=[l[:400] for l in raw.read_text(errors='replace').splitlines() if ('ERROR:'in l or 'FAIL:' in l or 'fatal:'in l or 'error:'in l) and 'api-key' not in l and '--remote_header' not in l]
            (d/'diagnostics').write_text('\n'.join(clean[-25:])+'\n')
    finally:
        if (d/'bep.jsonl').exists():(d/'bep.jsonl').unlink()
        restore('HEAD',[packet,migration]);row['finished']=now();(d/'arm.json').write_text(json.dumps(row,indent=2)+'\n')
    print(name,json.dumps(row),flush=True)
packet_native='//test/packetimpact/tests:tcp_rack_native_test'
packet_gvisor='//test/packetimpact/tests:tcp_rack_netstack_test'
case='TestSACKAtNextUnacknowledgedByte'
expected=['packet_boundary']
arm('packet_boundary','candidate',packet_gvisor,'wire',case)
summary={'sources':heads,'reference':'https://github.com/tamird/gvisor/actions/runs/41191724183/artifacts/13039706167','expected':expected,'executed':[k for k,x in results.items() if x.get('case_records',{}).get('executed')],'missing':[k for k in expected if not results.get(k,{}).get('case_records',{}).get('executed')],'failures':errors,'skips':{k:x.get('skipped_cases',[]) for k,x in results.items()},'arms':results,'execution_finished_at':max((v['ended'] for v in results.values() if 'ended' in v),default=None),'verdict_available_at':now()}
if all(results.get(k,{}).get('verified') for k in expected):
    summary['early_retransmissions']={k:{case:len(obs['retransmits']) for case,obs in results[k]['observations'].items()} for k in expected}
if not errors and results['packet_boundary'].get('verified'):
    with ZipFile(T/'control.zip') as z:
        prior=json.loads(z.read('verdict.json'))
    obs=results['packet_boundary']['observations']
    linux=prior['arms']['linux']['observations']
    def segments(row):
        return [(packet['start'],packet['size']) for packet in row['retransmits']]
    mismatches={case:{'actual':segments(val),'linux':segments(linux[case])} for case,val in obs.items() if val['mss']!=linux[case]['mss'] or segments(val)!=segments(linux[case])}
    if mismatches:
        errors.append('linux-wire-mismatch')
    summary['linux_comparison']={'control':'cd70edd2874764283ceace022211e3226ae068812430c685d8ee6ad0f25a31b6','mismatches':mismatches}
summary['verdict_available_at']=now()
(OUT/'verdict.json').write_text(json.dumps(summary,indent=2)+'\n')
sys.exit(bool(errors or summary['missing'] or any(summary['skips'].values())))
