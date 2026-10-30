import json,re,sys
from datetime import datetime,timezone
from pathlib import Path
d=Path(sys.argv[1]); errors=[]; arms={}
def read(p):return p.read_text().strip() if p.is_file() else ''
fixtures=[]
for arm,src in [('stock','d624fba03a2b3f1f9a314e54198c95460005cc15'),('fixed','c89f8f55abfe0d08c2bfc836192bd27d0aee7161')]:
    a=d/arm;text=read(a/'test.log')
    cases=dict((name,result) for result,name in re.findall(r'(?m)^\s*--- (PASS|FAIL|SKIP): TestNestedMemoryLimit/(\w+) ',text))
    assertions=re.findall(r'guest MemTotal = (\d+) bytes, want (\d+)',text)
    expected={'unlimited':'FAIL' if arm=='stock' else 'PASS','larger':'FAIL' if arm=='stock' else 'PASS','smaller':'PASS'}
    if cases!=expected or read(a/'source')!=src or read(a/'build.exit')!='0' or read(a/'owner.exit')!=('3' if arm=='stock' else '0') or not (a/'test.xml').is_file():
        errors.append('unexpected source/targets, missing results or wrong outcomes: '+arm)
    if arm=='stock' and (len(assertions)!=2 or any(want!='536870912' for _,want in assertions) or '805306368' not in [got for got,_ in assertions]):
        errors.append('stock did not fail on both guest limits as predicted')
    arms[arm]={'source':read(a/'source'),'targets':['//runsc:runsc','//test/root:root_test'],'cases':cases,'failures':[n for n,r in cases.items() if r=='FAIL'],'skips':[n for n,r in cases.items() if r=='SKIP'],'missing':sorted(set(expected)-cases.keys()),'guest_mismatches':assertions,'owner_exit':read(a/'owner.exit')}
    fixtures.append(read(a/'test.sha256'))
if not fixtures[0] or fixtures[0]!=fixtures[1]:errors.append('different guest test sources')
verdict={'verified':not errors,'errors':errors,'arms':arms,'finished':read(d/'end'),'available':datetime.now(timezone.utc).isoformat()}
(d/'verdict.json').write_text(json.dumps(verdict,indent=2)+'\n')
print(json.dumps(verdict));sys.exit(bool(errors))
