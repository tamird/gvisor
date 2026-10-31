from pathlib import Path
import hashlib
import json
import os
import re
import subprocess as sp
import sys
import traceback

from guard import guard
from prepare import adapt

tmp=Path(os.environ['RUNNER_TEMP'])
records=tmp/'records'
records.mkdir(exist_ok=True)
def save(name,obj):
    (records/name).write_text(json.dumps(obj,indent=2)+'\n')
def check(*args):
    return sp.check_output(list(args),text=True).strip()
def build(arm):
    b=str(tmp/'bazel-7.1.2')
    owner='//test/tailscale_replay:replay'
    flags=['--jobs=3','--nostamp']
    with (records/(arm+'-build.txt')).open('w') as log:
        rc=sp.run([b,'build',*flags,owner],stdout=log,stderr=sp.STDOUT,timeout=1450).returncode
    if rc:
        raise RuntimeError((arm,rc,(records/(arm+'-build.txt')).read_text()[-3500:]))
    files=check(b,'cquery',*flags,'--output=files',owner).splitlines()
    paths=[Path(f) for f in files if Path(f).is_file() and os.access(f,os.X_OK)]
    assert len(paths)==1,files
    out=tmp/('replay-'+arm);out.write_bytes(paths[0].read_bytes());out.chmod(0o755)
    return out

rows=[]
errors=[]
def replay(arm,exe,scenario,seed,ts=False,rack=True,loss=True,original=None):
    tag=f'{arm}-{scenario}-{seed}-ts{int(ts)}-rack{int(rack)}-loss{int(loss)}'
    out=records/(tag+'.txt')
    flags=['-cc=reno','-gso=false','-rack='+str(rack).lower(),'-client-timestamps='+str(ts).lower(),'-trials=1','-trace',f'-seed={seed}','-timeout=16s','-loss='+('burst' if loss else 'none')]
    if scenario=='tail-gap':
        flags+=['-burst-drop=100000','-burst-window=0','-burst-until-gap=3ms']
    elif scenario=='end-of-transfer':
        flags+=['-size=14400','-burst-keep-first=8','-burst-drop=4','-burst-window=0']
    if original:
        out=original
        rc=None  # The original verbatim process log is retained; this is readback, not execution.
    else:
        with out.open('w') as f:
            rc=sp.run([str(exe),*flags],stdout=f,stderr=sp.STDOUT,timeout=75).returncode
    try:
        txt=out.read_text()
        if original is None: assert rc==0,(rc,txt[-1000:])
        tf=lambda b:str(b).lower()
        assert f'ACTUAL cc=reno sack=true rack={tf(rack)} gso=off' in txt
        syn=re.findall(r'WIRE SYN ack=(true|false) ts=(true|false) sack=(true|false) mss=(\d+)',txt)
        assert {a for a,_,_,_ in syn}=={'true','false'} and all(t==tf(ts) and k=='true' and int(m)>0 for a,t,k,m in syn),syn
        progress=re.findall(r'PROGRESS measured=(true|false) received=(\d+) want=(\d+)',txt)
        assert len(progress)==3 and all(a=='false' and b==c for a,b,c in progress[:2]) and progress[-1][0]=='true',progress
        m=re.search(r'trial 1 fetch 3 measured\s+([0-9.]+) s \|\s*(\d+) data pkts,\s*(\d+) retransmitted \| RTOs\s*(\d+)([^\n]*)',txt)
        assert m,txt[-1500:]
        elapsed,n,retx,rtos,msg=m.groups()
        q,drop=map(int,re.search(r'path drops toward the client: (\d+) by the queue, (\d+) scripted',txt).groups())
        assert q==0 and (drop==0 if not loss else True)
        got,want=map(int,progress[-1][1:])
        if got!=want:
            assert re.search(r'(?:TIMED OUT|ERROR: read tcp 10\.9\.0\.2:\d+->10\.9\.0\.1:8080: i/o timeout)$',msg) and 15.90<=float(elapsed)<18,msg
        elif 'ERROR:' in msg or 'TIMED OUT' in msg:
            raise ValueError(msg)
        row=dict(origin="original-run-41164057010" if original else "current-execution",arm=arm,scenario=scenario,seed=seed,timestamps=ts,rack=rack,loss=loss,elapsed_s=float(elapsed),wire_data=int(n),retransmitted=int(retx),rtos=int(rtos),queue_drops=q,script_drops=drop,received=got,want=want,complete=got==want)
        if not loss: assert row['complete'] and not row['script_drops'], row
        rows.append(row);save('trials.json',rows)
        print(json.dumps(row),flush=True)
    except Exception as e:
        errors.append([tag,repr(e)]);print(tag,repr(e),flush=True)
        save('trial-errors.json',errors)


try:
    import zipfile
    root=Path.cwd()
    expected='e2f328e234de10bc67b1da05b3640b6a369f9cd5'
    original=tmp/'original'
    with zipfile.ZipFile(tmp/'original.zip') as z:
        assert all('/' not in n and n.endswith(('.txt','.json')) for n in z.namelist())
        z.extractall(original)
    v=json.loads((original/'verdict.json').read_bytes())
    assert v['status']=='FAIL' and v['trial_count']==6 and len(v['errors'])==11,v
    for f in sorted(original.glob('stock-*-ts*.txt')):
        case,seed,ts,rack,loss=re.fullmatch(r'stock-(.*)-(\d+)-ts([01])-rack([01])-loss([01])\.txt',f.name).groups()
        replay('stock',None,case,int(seed),bool(int(ts)),bool(int(rack)),bool(int(loss)),f)
    assert len(rows)==17 and not errors,errors
    default=[r for r in rows if r['scenario']=='default' and r['rack'] and not r['timestamps'] and r['loss']]
    assert len(default)==8 and all(not r['complete'] and r['rtos']==6 for r in default),default
    assert check('git','rev-parse','HEAD')==expected
    shas=adapt(tmp/'reporter.tar.gz',root/'test/tailscale_replay')
    assert shas==json.loads((original/'fixture-shas.json').read_bytes())
    save('fixture-shas.json',shas)
    arms=json.loads((Path(__file__).parent/'parents.json').read_bytes())
    arms['stock-repeat']=expected
    inputs={'exported':'64c016c92987cc04dfd4c7b091ddd21bdad875f8','original_archive':'13028531531','sources':arms,'binary_sha256':{}}
    save('inputs.json',inputs)
    exes={}
    for arm,src in arms.items():
        sp.run(['git','checkout','--quiet','--detach',src],check=True)
        assert check('git','rev-parse','HEAD')==src and not check('git','diff','--name-only',src)
        if arm!='stock-repeat':
            assert check('git','rev-parse','HEAD^')==expected,(arm,src)
            (records/(arm+'.patch')).write_text(check('git','diff',expected,src,'--','pkg/tcpip/transport/tcp')+'\n')
        exes[arm]=build(arm)
        inputs['binary_sha256'][arm]=hashlib.sha256(exes[arm].read_bytes()).hexdigest();save('inputs.json',inputs)
    # The four binaries run on this same worker. Rotate the execution order.
    # Each sees the same eight-byte request, loss schedule and jitter seed.
    sequence=['stock-repeat','guard16495','guard15432','guard16081']
    for i,(case,seed) in enumerate([('default',1),('default',2),('tail-gap',1)]):
        for arm in sequence[i:]+sequence[:i]:
            replay(arm,exes[arm],case,seed)
    assert len(rows)==29 and not errors,(len(rows),errors)
    save('verdict.json',{'status':'PASS','recovered_original_trials':17,'executed_matched_trials':12,'all_handshakes_and_payloads_checked':True})
except BaseException:
    save('verdict.json',{'status':'FAIL','trace':traceback.format_exc(),'trial_count':len(rows),'errors':errors})
    raise
