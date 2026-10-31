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
        expected=[case]+([case+'/'+c for c in ('repeat_high','repeat_low','keep_tail')] if kind=='wire' else [])
        checks=case_records(raw_log,expected)
        row['case_records']=checks; row['successful_cases']=len(checks['passed']); row['skipped_cases']=checks['skipped']
        if kind in ('wire','positive'):
            subprocess.run([sys.executable,str(T/('wire.py' if kind=='wire' else 'positive.py')),name,str(root),str(d)],check=True)
            obs=json.loads((d/'result.json').read_text())
            assert obs['observed'],obs
            if kind=='wire':
                flags=[obs['cases'][a]['tcp_info'][str(i)]['reord'] for a,i in [('repeat_high',3),('repeat_low',6)]]
                row['repeated_high_low']=flags;assert flags==[0,0] and code==0 and 'reneged third segment retransmitted and acknowledged' in log,(flags,code)
                # Independently locate the receiver's cumulative reneging ACK and
                # the retransmission of the previously SACKed third segment.
                from wire_decode import decode
                high=obs['cases']['repeat_high'];frames=decode((d/('trace-'+high['pcap_sha256'][:9]+'.pcap')).read_bytes())
                first=next(x for x in frames if x['tag']==1 and x['sack']);mss=high['mss'];ack=first['ack']
                marks={x['tag']:i for i,x in enumerate(frames) if x['tag'] in (3,11,12) and (x['sport'],x['dport'])==(first['sport'],first['dport'])}
                assert marks[3]<marks[11]<marks[12] and [(frames[marks[i]]['ack']-ack)%2**32 for i in (11,12)]==[2*mss,3*mss],marks
                sent=[i for i,x in enumerate(frames) if (x['sport'],x['dport'])==(first['dport'],first['sport']) and x['length']==mss and (x['seq']-ack)%2**32==2*mss]
                assert len([i for i in sent if i<marks[3]])==1 and any(marks[3]<i<marks[12] for i in sent), (sent,marks)
                assert not any(marks[3]<i<marks[11] for i in sent), ('resent packet 3 before the peer indicated it was missing',sent,marks)
                assert any(marks[11]<i<marks[12] for i in sent), ('failed to recover packet 3 after the receiver reneged',sent,marks)
                latency=next(frames[i]['ts'] for i in sent if marks[11]<i<marks[12])-frames[marks[11]]['ts']
                row['reneging_seconds']=latency
                # The receiver's RTT is 150ms. Original gVisor took ~500ms:
                # distinguish the half-RTT grace period from the full RTO.
                if key in ('linux','grace'):assert 0<latency<0.25,(key,latency)
                keep=obs['cases']['keep_tail']; fs=decode((d/('trace-'+keep['pcap_sha256'][:9]+'.pcap')).read_bytes())
                a=next(x for x in fs if x['tag']==20 and x['sack']); mss=keep['mss']; flow=(a['sport'],a['dport'])
                tags={x['tag']:i for i,x in enumerate(fs) if x['tag'] in (22,31,32,33) and (x['sport'],x['dport'])==flow}
                assert [tags[t] for t in (22,31,32,33)]==sorted(tags.values()) and len(tags)==4,tags
                assert [(fs[tags[t]]['ack']-a['ack'])%2**32 for t in (22,31,32,33)]==[mss,2*mss,5*mss,5*mss]
                assert fs[tags[32]]['ts']-fs[tags[31]]['ts']<0.04 and fs[tags[33]]['ts']-fs[tags[32]]['ts']>=0.29,tags
                first_mark=next(i for i,x in enumerate(fs) if x is a)
                repeats=[i for i,x in enumerate(fs) if first_mark<i<tags[33] and (x['sport'],x['dport'])==(flow[1],flow[0]) and x['length'] and (x['seq']-a['ack'])%2**32<5*mss and (x['seq']-a['ack'])%2**32+x['length']>2*mss]
                assert not repeats,(key,tags,repeats)
                row['kept_tail']={'repeat_frames':repeats,'ack_frames':tags,'interval':fs[tags[33]]['ts']-fs[tags[32]]['ts']}
                row['reneged_segment']={'previously_sacked':[2*mss,3*mss],'receiver_ack_before':2*mss,'receiver_ack_after':3*mss,'pcap_frames':sent,'ack_frames':marks}
            else:
                assert obs['expected_result'] and code==0,obs
                row['genuine_reorder']=obs['observations']
        elif kind=='pre':
            assert code==3 and 'ACK 3: TCP_INFO.ReordSeen = 1, want 0' in log and re.search(r'--- FAIL: '+case+r'\b',log),(code,log[-1200:])
        else:
            assert code==0 and row['successful_cases']>0,(code,log[-1200:])
            if case:assert re.search(r'--- PASS: '+case+r'\b',log) and case not in row['skipped_cases'],case
            if kind=='restore':
                row['credit']=re.findall(r'SACK_RESTORE stage=(\w+) count=(-?\d+)',log)
                assert row['credit']==[('before','1'),('reset','0'),('reinstate','1'),('drain','0')],row['credit']
        assert checks['verified'],checks
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
case='TestRACKRepeatedSACKAfterRTO'
arm('linux-tail','linux',packet_native,'wire','TestRACKRepeatedSACKAfterTimeout')
arm('retained-tail','candidate',packet_gvisor,'wire','TestRACKRepeatedSACKAfterTimeout')
arm('grace-tail','grace',packet_gvisor,'wire','TestRACKRepeatedSACKAfterTimeout')
arm('grace-positive','grace',packet_gvisor,'positive','TestRACKRepeatedLowerSACK')
expected=['linux-tail','retained-tail','grace-tail','grace-positive']
summary={'sources':heads,'reference':'https://github.com/tamird/gvisor/actions/runs/41088743068/artifacts/12994780498','expected':expected,'executed':[k for k,x in results.items() if x.get('case_records',{}).get('executed')],'missing':[k for k in expected if not results.get(k,{}).get('case_records',{}).get('executed')],'failures':errors,'skips':{k:x.get('skipped_cases',[]) for k,x in results.items()},'arms':results,'execution_finished_at':max((v['ended'] for v in results.values() if 'ended' in v),default=None),'verdict_available_at':now()}
if all('reneging_seconds' in results.get(k,{}) for k in ('retained-tail','grace-tail','linux-tail')):
    measured={k:results[k]['reneging_seconds'] for k in ('retained-tail','grace-tail','linux-tail')}
    summary['timing_comparison']={'seconds':measured,'retention_only_took_at_least_twice_grace':measured['retained-tail']>=2*measured['grace-tail']}
    if not summary['timing_comparison']['retention_only_took_at_least_twice_grace']:errors.append('timing hypothesis did not reproduce')
(OUT/'verdict.json').write_text(json.dumps(summary,indent=2)+'\n')
sys.exit(bool(errors or summary['missing'] or any(summary['skips'].values())))
