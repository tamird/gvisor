"""Publish only the two native owner identities, physical counts and fixed case."""
from hashlib import sha256
from pathlib import Path
from uuid import UUID
import json, re, sys, tarfile
import xml.etree.ElementTree as ET

SOURCE='f4605a0bc66962181c2c8e3146deaa06988fe023'
TREE='cb7fcf7f41e7505db3a1bc26e99479c2ab2dc22d'
OWNERS=('//pkg/sentry/vfs:vfs_test','//pkg/tcpip/transport/tcp:tcp_test')
FOCUS='TestRACKReorderingWindowSaturation'
def dig(v):
    assert isinstance(v,str) and re.fullmatch(r'[a-f0-9]{64}',v)
    return v
def checked(n):
    assert isinstance(n,int) and 0<=n<=1000000
    return n
def collect(key):
    task=Path(__file__).resolve().parent;sys.path.insert(0,str(task))
    import submit
    out={'source':SOURCE,'tree':TREE,'owners':{}}
    status=1
    try:
        code=submit.main(lambda _:key)
        r=json.loads((task/'receipt.json').read_bytes())
        for n in ('status','producer_status','collection_status'):
            v=r[n];assert v in ('PASS','FAIL','COMPLETE','INCOMPLETE');out[n]=v
        assert r['sources']==[SOURCE]
        p=task/'qualification.tar.gz';out['archive']={'bytes':p.stat().st_size,'sha256':dig(sha256(p.read_bytes()).hexdigest())}
        with tarfile.open(p) as t:
            get=lambda n:t.extractfile(n).read()
            pref='invocation-owners/'; ev=[json.loads(l) for l in get(pref+'events.jsonl').splitlines()]
            [child]=[v['started']['uuid'] for v in ev if 'started' in v];out['child']=str(UUID(child))
            result=json.loads(get('weekly-owners.json'));assert result['source']==SOURCE and result['tree']==TREE
            assert set(result['owners'])==set(OWNERS)
            for name in OWNERS:
                item=result['owners'][name]
                a=item['action'];assert a['runner']=='remote' and not a.get('cacheHit') and a['exitCode']==0 and item['native_status']=='PASSED'
                refs={ref['name']:ref for ref in item['primary']}
                ref=refs['test.xml'];xml=get(pref+ref['path']);assert sha256(xml).hexdigest()==dig(ref['sha256'])
                nodes=ET.fromstring(xml).findall('.//testcase')
                assert len(nodes)==item['cases'] and not any(n.find('failure') is not None or n.find('error') is not None for n in nodes)
                if name==OWNERS[1]:assert len([n for n in nodes if n.get('name')==FOCUS and n.find('skipped') is None])==1
                out['owners'][name]={'cases':checked(item['cases']),'skips':checked(item['skips']),'action':dig(a['digest']['hash']),'xml':dig(ref['sha256']),'saturation':bool(item['saturation'])}
        status=code
    except Exception as e:
        out['error']=type(e).__name__ if type(e).__name__ in ('KeyError','AssertionError','ValueError','FileNotFoundError','StopIteration','TypeError') else 'OTHER'
        tb=e.__traceback__;out['lines']=[]
        while tb:
            if tb.tb_frame.f_code.co_filename.endswith('receipt.py'):out['lines'].append(tb.tb_lineno)
            tb=tb.tb_next
    finally:
        key=None;m=task/'submission.json'
        if m.is_file():out['invocation']=str(UUID(json.loads(m.read_bytes())['invocationId']))
        (task.parent/'verdict.json').write_text(json.dumps(out,indent=2)+'\n')
    raise SystemExit(status)
