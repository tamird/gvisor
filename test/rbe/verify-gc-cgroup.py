import json, re, sys
from datetime import datetime, timezone
from pathlib import Path
d=Path(sys.argv[1])
limits={'.':'201326592','open':'max','open/unbounded':'max','open/large':'536870912','open/small':'134217728'}
errors=[]
rows={}
proof=d/'kernel-limits.txt'
if not proof.exists() or dict(line.split() for line in proof.read_text().splitlines())!=limits:
    errors.append('host did not establish all five real kernel ceilings')
for name,expected,src in [('original',3,'f6310f08dad4edbde042828e20090d1079e93967'),('corrected',0,'96f87f902f8b2fb3383b5609aa93ad5b896563f1')]:
    a=d/name
    obs={}
    def read(k): return (a/k).read_text().strip() if (a/k).is_file() else ''
    if read('source')!=src or read('exit')!=str(expected):
        errors.append(f'{name}: source missing or unexpected Bazel status {read("exit")}')
    log=read('test.log')
    for case,want in zip(('unbounded','large','small'), (1073741824,268435456,67108864) if name=='original' else (100663296,100663296,67108864)):
        matches=re.findall(r'cgroup report=(\d+), host=(\d+), effective cgroup ceiling=(\d+), actual GC soft limit=(\d+)',log)
        bounds=[tuple(map(int,m)) for m in matches]
        status='FAIL' if name=='original' and case!='small' else 'PASS'
        ix=('unbounded','large','small').index(case)
        if len(bounds)!=3 or bounds[ix][3]!=want or not re.search(r'--- '+status+r': TestAncestorMemoryBudget/'+case+r'(?:\s|$)',log):
            errors.append(f'{name}/{case}: missing expected GC soft limit {want} or test status {status}')
        else:
            obs[case]=dict(zip(('reported','host','effective','soft_limit'),bounds[ix]))
    rows[name]={'source':src,'observations':obs,'end':read('end'),'fixture':read('fixture-sha256')}
if rows['original']['fixture']!=rows['corrected']['fixture'] or not rows['original']['fixture']:
    errors.append('fixtures not identical')
r={'errors':errors,'sources':rows,'verdict_at':datetime.now(timezone.utc).isoformat()}
(d/'verdict.json').write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps(r,indent=2))
sys.exit(bool(errors))
