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
def replay(arm,exe,scenario,seed,ts=False,rack=True,loss=True):
    tag=f'{arm}-{scenario}-{seed}-ts{int(ts)}-rack{int(rack)}-loss{int(loss)}'
    out=records/(tag+'.txt')
    flags=['-cc=reno','-gso=false','-rack='+str(rack).lower(),'-client-timestamps='+str(ts).lower(),'-trials=1','-trace',f'-seed={seed}','-timeout=16s','-loss='+('burst' if loss else 'none')]
    if scenario=='tail-gap':
        flags+=['-burst-drop=100000','-burst-window=0','-burst-until-gap=3ms']
    elif scenario=='end-of-transfer':
        flags+=['-size=14400','-burst-keep-first=8','-burst-drop=4','-burst-window=0']
    with out.open('w') as f:
        rc=sp.run([str(exe),*flags],stdout=f,stderr=sp.STDOUT,timeout=75).returncode
    try:
        txt=out.read_text();assert rc==0,(rc,txt[-1000:])
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
        if got!=want: assert 'TIMED OUT' in msg, msg
        row=dict(arm=arm,scenario=scenario,seed=seed,timestamps=ts,rack=rack,loss=loss,elapsed_s=float(elapsed),wire_data=int(n),retransmitted=int(retx),rtos=int(rtos),queue_drops=q,script_drops=drop,received=got,want=want,complete=got==want)
        if not loss: assert row['complete'] and not row['script_drops'], row
        rows.append(row);save('trials.json',rows)
        print(json.dumps(row),flush=True)
    except Exception as e:
        errors.append([tag,repr(e)]);print(tag,repr(e),flush=True)
        save('trial-errors.json',errors)

try:
    root=Path.cwd();src=check('git','rev-parse','HEAD')
    assert src=='e2f328e234de10bc67b1da05b3640b6a369f9cd5'
    assert not check('git','diff','--name-only',src)
    shas=adapt(tmp/'reporter.tar.gz',root/'test/tailscale_replay')
    save('fixture-shas.json',shas)
    base=build('stock')
    inputs={'bazel_source':src,'exported_go_source':'64c016c92987cc04dfd4c7b091ddd21bdad875f8','reporter':'ad90750cf030c4e56a83cdba8ae1d4f3c1ff75c8','stock_binary_sha256':hashlib.sha256(base.read_bytes()).hexdigest()}
    save('inputs.json',inputs)
    replay('stock',base,'default',1,loss=False)
    assert not errors,'baseline preflight'
    # Keep the reporter's normal two full warm-ups, 3s idle, 3ms path RTT,
    # 80Mbps and 200us jitter. Compare matching jitter seeds in each arm.
    for seed in range(1,9):
        replay('stock',base,'default',seed)
    for case in ('tail-gap','end-of-transfer'):
        for seed in (1,2):
            replay('stock',base,case,seed)
    # Choose up to three actual observed deadline misses. Control arms have
    # identical settings apart from exactly one fixture knob or source guard.
    miss=[r for r in rows if r['loss'] and not r['complete']]
    selected=[]
    for case in ('default','tail-gap','end-of-transfer'):
        rr=next((r for r in miss if r['scenario']==case),None)
        if rr is not None: selected.append((case,rr['seed']))
    if not selected: selected=[('default',1),('end-of-transfer',1)]
    for case,seed in selected:
        replay('stock',base,case,seed,ts=True)
        replay('stock',base,case,seed,rack=False)
    if miss:
        guard(root)
        diff=check('git','diff','--','pkg/tcpip/transport/tcp')
        (records/'single-16495-port.patch').write_text(diff+'\n')
        patched=build('guard16495')
        inputs['guard_binary_sha256']=hashlib.sha256(patched.read_bytes()).hexdigest();save('inputs.json',inputs)
        for case,seed in selected:
            replay('guard16495',patched,case,seed)
            replay('stock-repeat',base,case,seed)
    assert len([r for r in rows if r['arm']=='stock' and r['scenario']=='default' and r['loss'] and r['rack'] and not r['timestamps']])==8
    assert not errors,errors
    save('verdict.json',{'status':'PASS','experiment_complete':True,'trials_executed':len(rows),'stock_deadlines':len(miss),'conditional_guard_tested':bool(miss)})
except BaseException:
    save('verdict.json',{'status':'FAIL','trace':traceback.format_exc(),'trial_count':len(rows),'errors':errors})
    raise
