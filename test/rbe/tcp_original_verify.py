import hashlib,json,os,re,sys
from pathlib import Path
from zipfile import ZipFile

p=Path(sys.argv[1]);destination=Path(sys.argv[2])
digest=hashlib.sha256(p.read_bytes()).hexdigest()
assert digest=='59747cac54b5cdc724466b579427e641bd644202fbf23a02b72d934e4ef4a588',digest
result={'artifact':'https://github.com/tamird/gvisor/actions/runs/41088743068/artifacts/12994780498','sha256':digest,'arms':{}}
with ZipFile(p) as zipped:
    original=json.loads(zipped.read('verdict.json'))
    for name,row in original['arms'].items():
        item={key:row[key] for key in ['source','target','case','started','ended','exit','invocation']}
        if name=='live-restore':
            item['original_failure']=zipped.read(name+'/diagnostics').decode().strip()
            assert row['exit']==1
        else:
            log=zipped.read(name+'/test.log').decode().replace('\x16','')
            item['pass_entries']=len(re.findall(r'(?m)^\s*--- PASS: ',log))
            item['skip_entries']=re.findall(r'(?m)^\s*--- SKIP: (\S+)',log)
            item['unexpected_fail_entries']=re.findall(r'(?m)^\s*--- FAIL: (\S+)',log)
            if name=='original-compact':
                assert row['exit']==3 and 'ACK 3: TCP_INFO.ReordSeen = 1, want 0' in log and item['unexpected_fail_entries']==['TestRACKRepeatedSACKAfterRTO']
                item['expected_failure']=item.pop('unexpected_fail_entries').pop()
            else:
                assert row['exit']==0 and item['pass_entries']>0 and not item['unexpected_fail_entries'] and not item['skip_entries']
                if row['case']:
                    assert re.search(r'(?m)^\s*--- PASS: '+re.escape(row['case'])+r' ',log)
            if name.endswith('-wire') or name=='combined-positive':
                detail=json.loads(zipped.read(name+'/result.json'))
                assert row['verified'] and detail['observed']
                captures=[f for f in zipped.namelist() if f.startswith(name+'/trace') and f.endswith('.pcap')]
                item['captures']={f:hashlib.sha256(zipped.read(f)).hexdigest() for f in captures}; assert item['captures']
                for key in ('repeated_high_low','reneged_segment','genuine_reorder'):
                    if key in row:item[key]=row[key]
        result['arms'][name]=item
    assert len(result['arms'])==11
destination.write_text(json.dumps(result,indent=2)+'\n')
