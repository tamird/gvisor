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
        paths=[migration,'pkg/tcpip/transport/tcp/state.go'];restore(carrier,paths)
        changed=git('diff','--name-only','HEAD').decode().splitlines()
        assert sorted(changed)==sorted(paths),(src,changed,paths)
        row['extra_test_files']=changed
        flags=['bazelisk','--bazelrc='+os.environ['FIFO_RC'],'test','--config=x86_64','--strategy=TestRunner=local','--spawn_strategy=linux-sandbox','--jobs=4','--test_env=GO_TEST_WRAP_TESTV=1','--test_env=GVISOR_TEST_CLOCK_SOURCE=calibrated','--test_env=XTABLES_LOCKFILE','--define=gvisor_test_clock_source=calibrated','--strip=never','--test_sharding_strategy=disabled','--nocache_test_results','--test_output=all','--test_timeout=600','--build_event_json_file='+str(d/'bep.jsonl'),'--build_event_json_file_path_conversion=false']
        flags+=['--test_filter=^Test(SACKPackets|RACKObservation)AfterLiveRestore$']
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
        log=(root/'test.log').read_text();(d/'test.log').write_text(log);log=log.replace('\x16','')
        row['pass']=re.findall(r'(?m)^\s*--- PASS: (\S+)',log)
        row['fail']=re.findall(r'(?m)^\s*--- FAIL: (\S+)',log)
        row['skip']=re.findall(r'(?m)^\s*--- SKIP: (\S+)',log)
        row['credit_and_rack']=re.findall(r'SACK_RESTORE rack=(\w+) stage=(\w+) count=(\d+) reord=(\w+)',log)
        assert not row['skip'] and 'TestSACKPacketsAfterLiveRestore' in row['pass'],row
        assert [(i[1],i[2],i[3]) for i in row['credit_and_rack'] if i[0]=='false']==[('before','1','false'),('reset','0','false'),('reinstate','1','false'),('drain','0','false')],row
        probes=[i[1:] for i in row['credit_and_rack'] if i[0]=='true']
        assert probes[:2]==[('before','2','false'),('reset','0','false')] and len(probes)==4 and probes[3][:2]==('drain','0'),row
        if key=='before':
            assert code==3 and row['fail']==['TestRACKObservationAfterLiveRestore'] and 'reinstate reordering=true, want false' in log and probes[2]==('reinstate','1','true'),row
        else:
            assert code==0 and not row['fail'] and 'TestRACKObservationAfterLiveRestore' in row['pass'] and probes[2]==('reinstate','1','false'),row
        row['verified']=True
    except Exception as err:
        row['verified']=False;row['error']=repr(err)[:1900];errors.append(name)
        raw=T/(name+'.log')
        if raw.exists():
            clean=[l[:900] for l in raw.read_text(errors='replace').splitlines() if any(m in l for m in ('ERROR:', 'FAIL:', 'fatal:', 'error:', 'undefined:', '.go:', 'compilepkg:', 'missing strict dependencies')) and 'api-key' not in l and '--remote_header' not in l and '--bes_header' not in l]
            (d/'diagnostics').write_text('\n'.join(clean[-25:])+'\n')
    finally:
        if (d/'bep.jsonl').exists():(d/'bep.jsonl').unlink()
        restore('HEAD',[migration,'pkg/tcpip/transport/tcp/state.go']);row['finished']=now();(d/'arm.json').write_text(json.dumps(row,indent=2)+'\n')
    print(name,json.dumps(row),flush=True)
for key in ['before','combined']:
    arm('restore-'+key,key,'//pkg/tcpip/transport/tcp/test/e2e:tcp_test','restore')
summary={'sources':heads,'original':json.loads((OUT/'original-rechecked.json').read_text()),'restore':results,'errors':errors}
(OUT/'verdict.json').write_text(json.dumps(summary,indent=2)+'\n');sys.exit(bool(errors))
