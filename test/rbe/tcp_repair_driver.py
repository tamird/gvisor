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
        log=(root/'test.log').read_text(); row['successful_cases']=len(re.findall(r'(?m)^\s*--- PASS: ',log)); row['skipped_cases']=re.findall(r'(?m)^\s*--- SKIP: (\S+)',log)
        (d/'test.log').write_text(log)
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
arm('native-compact','standalone',packet_native,'compact',case)
arm('original-compact','before',packet_gvisor,'pre',case)
arm('fixed-compact','standalone',packet_gvisor,'compact',case)
arm('linux-wire','combined',packet_native,'wire','TestRACKRepeatedSACKAfterTimeout')
arm('combined-wire','combined',packet_gvisor,'wire','TestRACKRepeatedSACKAfterTimeout')
arm('combined-positive','combined',packet_gvisor,'positive','TestRACKRepeatedLowerSACK')
for target in ['//pkg/tcpip/transport/tcp:tcp_test','//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test','//pkg/tcpip/transport/tcp/test/e2e:tcp_sack_test',packet_gvisor]:
    arm('suite-'+target.split(':')[1],'standalone',target,'suite')
arm('live-restore','combined','//pkg/tcpip/transport/tcp/test/e2e:tcp_test','restore','TestSACKPacketsAfterLiveRestore')
summary={'sources':heads,'earlier_timeout':'https://github.com/tamird/gvisor/actions/runs/41085114274/artifacts/12991273249','earlier_genuine_linux':'https://github.com/tamird/gvisor/actions/runs/41078201696/artifacts/12989451780','arms':results,'errors':errors}
(OUT/'verdict.json').write_text(json.dumps(summary,indent=2)+'\n');sys.exit(bool(errors))
