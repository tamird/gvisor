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
        cases=[(sz,r,m) for sz in (2000,1900) for r in ('packet','suffix') for m in ('rack','rfc6675')]
        expected=[case]+[f'{case}/{sz}_{r}_{m}' for sz,r,m in cases]
        checks=case_records(raw_log,expected)
        row['case_records']=checks;row['skipped_cases']=checks['skipped']
        assert checks['verified'] and code==0 and result['status']=='PASSED',(code,checks)
        traces=re.findall(r'GSO_TRACE size=(\d+) report=(\w+) mode=(\w+) offset=(-?\d+) bytes=(\d+)',log)
        seen={(int(sz),r,m):(int(o),int(b)) for sz,r,m,o,b in traces}
        assert len(traces)==len(cases) and set(seen)==set(cases),traces
        row['observed']={f'{sz}_{r}_{m}':vals for (sz,r,m),vals in seen.items()}
        for (sz,r,m),got in seen.items():
            want=(1501,sz-1501) if name=='timer' and r=='packet' else (-1,0)
            assert got==want,(sz,r,m,got,want)
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
target='//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test'
case='TestSACKHeadAfterGSOWrite'
arms=['control','timer']
for key in arms:arm(key,key,target,'wire',case)
v={'expected':arms,'executed':[k for k in arms if results[k].get('case_records',{}).get('executed')],'missing':[k for k in arms if not results[k].get('case_records',{}).get('executed')],'skips':{k:r.get('skipped_cases') for k,r in results.items()},'failures':errors,'arms':results,'execution_finished_at':max((r['ended'] for r in results.values() if 'ended' in r),default=None),'verdict_available_at':now()}
(OUT/'verdict.json').write_text(json.dumps(v,indent=2)+'\n')
sys.exit(bool(errors or v['missing'] or any(v['skips'].values())))
