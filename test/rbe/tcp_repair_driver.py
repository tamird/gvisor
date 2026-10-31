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
        paths=[migration,'pkg/tcpip/transport/tcp/endpoint.go'];restore(carrier,paths)
        # This negative control reproduces the old true bit at this particular
        # restore, after (not instead of) the normal range/pcount reset.
        if name=='stale-head':
            ep=Path('pkg/tcpip/transport/tcp/endpoint_state.go')
            content=ep.read_text();assert content.count('e.scoreboard.Reset()')==1
            ep.write_text(content.replace('e.scoreboard.Reset()', 'e.scoreboard.Reset()\n\t\te.scoreboard.headPacketSACKed = true'))
            paths.append(str(ep))
        changed=git('diff','--name-only','HEAD').decode().splitlines()
        assert sorted(changed)==sorted(paths),(changed,paths)
        row['overlays']=paths
        row['fixture_sha256']=hashlib.sha256(Path(migration).read_bytes()).hexdigest()
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
        expected=[case,case+'/unsaved',case+'/restored'];checks=case_records(raw_log,expected)
        row['case_records']=checks;row['skipped_cases']=checks['skipped']
        traces=re.findall(r'RESTORE_WIRE stage=([a-z-]+) tick=(-?[0-9]+) offset=(-?[0-9]+) bytes=([0-9]+)',log)
        seen={label:[int(k),int(o),int(n)] for label,k,o,n in traces}
        row['wire']=seen
        assert checks['verified'] and code==0 and result['status']=='PASSED',(code,checks)
        expect={'unsaved-whole':(-1 if name=='parent' else 1500),'restored-suffix':(1501 if name=='stale-head' else -1)}
        if name!='stale-head':expect['restored-fresh']=(-1 if name=='parent' else 2500)
        assert len(traces)==len(expect) and set(seen)==set(expect),seen
        for label,off in expect.items():
            k,o,n=seen[label]
            assert (k,o,n)==(-1,-1,0) if off==-1 else (0<k<=40 and o==off and n>0),(name,label,seen)
        row['verified']=True
    except Exception as err:
        row['verified']=False;row['error']=repr(err)[:1900];errors.append(name)
        raw=T/(name+'.log')
        if raw.exists():
            clean=[l[:400] for l in raw.read_text(errors='replace').splitlines() if ('ERROR:'in l or 'FAIL:' in l or 'fatal:'in l or 'error:'in l or 'mismatched types' in l or 'cannot use' in l) and 'api-key' not in l and '--remote_header' not in l]
            (d/'diagnostics').write_text('\n'.join(clean[-25:])+'\n')
    finally:
        if (d/'bep.jsonl').exists():(d/'bep.jsonl').unlink()
        restore('HEAD',[migration,'pkg/tcpip/transport/tcp/endpoint.go','pkg/tcpip/transport/tcp/endpoint_state.go']);row['finished']=now();(d/'arm.json').write_text(json.dumps(row,indent=2)+'\n')
    print(name,json.dumps(row),flush=True)
target='//pkg/tcpip/transport/tcp/test/e2e:tcp_test'
case='TestRenegingHeadAcrossLiveRestore'
arms=['parent','timer','stale-head']
for key in arms:arm(key,key,target,'restore',case)
v={'expected':arms,'executed':[k for k in arms if results[k].get('case_records',{}).get('executed')],'missing':[k for k in arms if not results[k].get('case_records',{}).get('executed')],'skips':{k:r.get('skipped_cases') for k,r in results.items()},'failures':errors,'arms':results,'execution_finished_at':max((r['ended'] for r in results.values() if 'ended' in r),default=None),'verdict_available_at':now()}
(OUT/'verdict.json').write_text(json.dumps(v,indent=2)+'\n')
sys.exit(bool(errors or v['missing'] or any(v['skips'].values())))
