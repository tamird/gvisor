import hashlib,json,os,re,subprocess,sys
from pathlib import Path
from datetime import datetime,timezone
from zipfile import ZipFile
T=Path(os.environ['RUNNER_TEMP']); OUT=T/'replay-public'; carrier=os.environ['GITHUB_SHA']
heads=json.loads((T/'tcp-heads.json').read_text())
packet='pkg/tcpip/transport/tcp/test/e2e/tcp_rack_test.go'
migration='pkg/tcpip/transport/tcp/test/e2e/tcp_migration_test.go'
results={};errors=[]
def now():return datetime.now(timezone.utc).isoformat()
def git(*args):return subprocess.check_output(['git',*args])
def restore(source,paths):git('restore','--source',source,'--worktree','--',*paths)
def arm(name,key,target,kind,case=None):
    src=heads[key];d=OUT/name;d.mkdir()
    row=results[name]={'source':src,'target':target,'case':case,'started':now()}
    try:
        production=['pkg/tcpip/transport/tcp/snd.go','pkg/tcpip/transport/tcp/sack_scoreboard.go']
        assert git('rev-parse','HEAD').decode().strip()==carrier
        restore(src,production)
        git('diff','--exit-code',src,'--','pkg','runsc','tools','MODULE.bazel','test/benchmarks/tcp/tcp_proxy.go','test/benchmarks/tcp/xdp.go')
        row['proxy_blob']=git('rev-parse',src+':test/benchmarks/tcp/tcp_proxy.go').decode().strip()
        row['fixture_blob']=git('rev-parse',carrier+':test/benchmarks/tcp/receiver_transfer.py').decode().strip()
        flags=['bazelisk','--bazelrc='+os.environ['FIFO_RC'],'test','--config=x86_64','--strategy=TestRunner=local','--spawn_strategy=linux-sandbox','--jobs=4','--test_env=GO_TEST_WRAP_TESTV=1','--test_env=GVISOR_TEST_CLOCK_SOURCE=calibrated','--test_env=XTABLES_LOCKFILE','--define=gvisor_test_clock_source=calibrated','--strip=never','--test_sharding_strategy=disabled','--nocache_test_results','--test_output=all','--test_timeout=600','--build_event_json_file='+str(d/'bep.jsonl'),'--build_event_json_file_path_conversion=false']
        if case:flags+=['--test_filter=^'+case+'$']
        raw=T/(name+'.log')
        with raw.open('wb') as output:code=subprocess.run([*flags,target],stdout=output,stderr=subprocess.STDOUT).returncode
        row['exit']=code;row['ended']=now()
        (d/'source').write_text(src+'\n');(d/'exit').write_text(str(code)+'\n')
        root=Path('bazel-testlogs')/target[2:].replace(':','/')
        events=[json.loads(l) for l in (d/'bep.jsonl').read_text().splitlines()]
        row['invocation']=next(e['started']['uuid'] for e in events if 'started' in e)
        rows=[e['testResult'] for e in events if 'testResult'in e]; row['bep_attempts']=[r['status'] for r in rows]
        if (root/'test.log').exists():(d/'test.log').write_bytes((root/'test.log').read_bytes())
        assert len(rows)==1,len(rows)
        result=rows[0]
        assert not result.get('cachedLocally') and not result['executionInfo'].get('cachedRemotely') and result['executionInfo']['strategy'] in ('local','standalone'),result['executionInfo']
        from case_records import case_records
        raw_log=(root/'test.log').read_bytes()
        (d/'test.log').write_bytes(raw_log)
        log=raw_log.decode().replace('\x16','')
        cases=('control1','shrink1','shrink2','control2')
        expected=['TestReceiverTransfer/'+k for k in cases]
        checks=case_records(raw_log,expected);row['case_records']=checks;row['skipped_cases']=checks['skipped']
        for p in (root/'test.outputs').glob('*'):
            if p.suffix in ('.json','.pcap') or p.name.endswith('.proxy.log'):
                (d/p.name).write_bytes(p.read_bytes())
        for p in root.rglob('outputs.zip'):
            with ZipFile(p) as z:
                for file in z.namelist():
                    if file.endswith(('.json','.proxy.log','.pcap')):
                        (d/Path(file).name).write_bytes(z.read(file))
        assert checks['verified'] and code==0 and result['status']=='PASSED',(code,checks)
        row['observed']={}
        for k in cases:
            report=json.loads((d/(k+'.json')).read_bytes())
            assert report['received']==15360 and report['sacked']==list(range(1,32,2)) and report['wire']
            gate=next(time for time,op,*_ in report['wire'] if op=='gate')
            assert gate==report['gate_us']
            resent=[i for event in report['wire'] if event[1]=='send' for time,_,i,again in [event] if time>=gate and i%2 and again]
            row['observed'][k]={'delivery_us':report['delivery_us'],'sha256':report['sha256'],'odd_resent':resent,'rcvbuf':report['rcvbuf']}
        row['verified']=True
    except Exception as err:
        row['verified']=False;row['error']=repr(err)[:1900];errors.append(name)
        raw=T/(name+'.log')
        if raw.exists():
            clean=[l[:400] for l in raw.read_text(errors='replace').splitlines() if ('ERROR:'in l or 'FAIL:' in l or 'fatal:'in l or 'error:'in l or 'mismatched types' in l or 'cannot use' in l) and 'api-key' not in l and '--remote_header' not in l]
            (d/'diagnostics').write_text('\n'.join(clean[-25:])+'\n')
    finally:
        if (d/'bep.jsonl').exists():(d/'bep.jsonl').unlink()
        restore(carrier,['pkg/tcpip/transport/tcp/snd.go','pkg/tcpip/transport/tcp/sack_scoreboard.go']);row['finished']=now();(d/'arm.json').write_text(json.dumps(row,indent=2)+'\n')
    print(name,json.dumps(row),flush=True)
target='//test/benchmarks/tcp:receiver_transfer'
arms=['control','timer']
for key in arms:arm(key,key,target,'real')
v={'expected':arms,'executed':[k for k in arms if results[k].get('case_records',{}).get('executed')],'missing':[k for k in arms if not results[k].get('case_records',{}).get('executed')],'skips':{k:r.get('skipped_cases') for k,r in results.items()},'failures':errors,'arms':results,'execution_finished_at':max((r['ended'] for r in results.values() if 'ended' in r),default=None),'verdict_available_at':now()}
(OUT/'verdict.json').write_text(json.dumps(v,indent=2)+'\n')
sys.exit(bool(errors or v['missing'] or any(v['skips'].values())))
