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
        production=['pkg/tcpip/link/sniffer/sniffer.go', 'pkg/tcpip/transport/packet/endpoint.go']
        assert git('rev-parse','HEAD').decode().strip()==carrier
        restore(src,production)
        git('diff','--exit-code',src,'--','pkg','runsc','tools',':(exclude)tools/gvisor2pcap/gvisor2pcap_test.go')
        assert git('hash-object','tools/gvisor2pcap/gvisor2pcap_test.go')==git('rev-parse',carrier+':tools/gvisor2pcap/gvisor2pcap_test.go')
        flags=['bazelisk','--bazelrc='+os.environ['FIFO_RC'],'test','--config=x86_64','--strategy=TestRunner=local','--spawn_strategy=linux-sandbox','--jobs=4','--test_env=GO_TEST_WRAP_TESTV=1','--test_env=GVISOR_TEST_CLOCK_SOURCE=calibrated','--test_env=XTABLES_LOCKFILE','--define=gvisor_test_clock_source=calibrated','--strip=never','--test_sharding_strategy=disabled','--nocache_test_results','--test_output=all','--test_timeout=600','--build_event_json_file='+str(d/'bep.jsonl'),'--build_event_json_file_path_conversion=false']
        if kind=='bench':flags+=['--test_arg=-test.run=^$','--test_arg=-test.bench=^BenchmarkBinaryCapture$','--test_arg=-test.benchtime=150ms','--test_arg=-test.cpu=1','--test_arg=-test.benchmem']
        elif case:flags+=['--test_filter=^'+case+'$']
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
        if kind=='bench':
            pattern=r'^BenchmarkBinaryCapture/(64|1500|9000)(?:-1)?\s+\d+\s+([.0-9]+) ns/op\s+(\d+) B/op\s+(\d+) allocs/op'
            data=re.findall(pattern,log,re.M)
            assert code==0 and result['status']=='PASSED' and sorted(n for n,*_ in data)==['1500','64','9000'],(code,data,log[-400:])
            row['bench']=data;row['case_records']={'executed':['BenchmarkBinaryCapture/'+n for n,*_ in data]}
        else:
            checks=case_records(raw_log,[case]);row['case_records']=checks;row['skipped_cases']=checks['skipped']
            if key=='control':
                assert code==3 and result['status']=='FAILED' and checks['executed']==checks['failed']==[case] and not checks['skipped'] and 'packet capture replaced the borrowed Ethernet header' in log,(code,checks)
            else:assert code==0 and result['status']=='PASSED' and checks['verified'],(code,checks)
        row['verified']=True
    except Exception as err:
        row['verified']=False;row['error']=repr(err)[:1900];errors.append(name)
        raw=T/(name+'.log')
        if raw.exists():
            clean=[l[:400] for l in raw.read_text(errors='replace').splitlines() if ('ERROR:'in l or 'FAIL:' in l or 'fatal:'in l or 'error:'in l or 'mismatched types' in l or 'cannot use' in l) and 'api-key' not in l and '--remote_header' not in l]
            (d/'diagnostics').write_text('\n'.join(clean[-25:])+'\n')
    finally:
        if (d/'bep.jsonl').exists():(d/'bep.jsonl').unlink()
        restore(carrier,['pkg/tcpip/link/sniffer/sniffer.go', 'pkg/tcpip/transport/packet/endpoint.go']);row['finished']=now();(d/'arm.json').write_text(json.dumps(row,indent=2)+'\n')
    print(name,json.dumps(row),flush=True)
target='//tools/gvisor2pcap:gvisor2pcap_test'
arms=[]
for key in ('control','fixed'):
    arms.append(key);arm(key,key,target,'regression','TestCaptureNetworkBoundary')
for n in range(8):
    for key in (('control','fixed') if n%2==0 else ('fixed','control')):
        name=f'{key}-{n+1}';arms.append(name);arm(name,key,target,'bench')
v={'expected':['control','fixed']+[f'{key}-{n}' for n in range(1,9) for key in ('control','fixed')],'executed':[k for k in arms if results[k].get('case_records',{}).get('executed')],'missing':[k for k in arms if not results[k].get('case_records',{}).get('executed')],'skips':{k:r.get('skipped_cases') for k,r in results.items()},'failures':errors,'arms':results,'execution_finished_at':max((r['ended'] for r in results.values() if 'ended' in r),default=None),'verdict_available_at':now()}
v['missing']=sorted(set(v['expected'])-set(v['executed']))
(OUT/'verdict.json').write_text(json.dumps(v,indent=2)+'\n')
sys.exit(bool(errors or v['missing'] or any(v['skips'].values())))
