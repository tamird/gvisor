from pathlib import Path
from datetime import datetime,timezone
import json,re,sys
d=Path(sys.argv[1]);errors=[];results={};sha=set()
def read(p):return p.read_text() if p.is_file() else ''
try:prior=json.loads(read(d/'guests.json'))
except ValueError:prior={}
for arm,src in [('stock','d624fba03a2b3f1f9a314e54198c95460005cc15'),('fixed','c89f8f55abfe0d08c2bfc836192bd27d0aee7161')]:
    a=d/arm;guests=prior.get('arms',{}).get(arm,{})
    checks={};log=read(a/'test.log').replace('\x16','')
    if guests.get('source')!=src or guests.get('executed_cases')!=3 or read(a/'source').strip()!=src:errors.append(arm+' source identity or guest results')
    sha.add(read(a/'test.sha256').strip())
    for name in ['unlimited','larger','smaller']:
        g=guests.get('results',{}).get(name,{})
        want={'unlimited':536870912,'larger':536870912,'smaller':402653184}[name]
        if arm=='stock' and name=='larger':want=805306368
        size=g.get('MemTotal')
        if size is None or (size<=536870912 if arm=='stock' and name=='unlimited' else size!=want) or g.get('status')!=0:errors.append(arm+' '+name+' incorrect OCI observation')
        verdict=re.findall(r'--- (PASS|FAIL|SKIP): TestNestedMemoryLimit/'+name+r'\s',log)
        if verdict!=['FAIL' if arm=='stock' and name!='smaller' else 'PASS']:errors.append(arm+' '+name+' missing or incorrect canonical test')
        checks[name]={'guest_MemTotal':size,'owner_results':verdict}
    if arm=='stock' and (len(re.findall('guest MemTotal = [0-9]+ bytes, want 536870912',log))!=2):errors.append('stock must fail both intended assertions')
    if read(a/'owner.exit').strip()!=('3' if arm=='stock' else '0'):errors.append(arm+' canonical target exit')
    results[arm]={'source':src,'cases':checks}
if len(sha)!=1 or '' in sha:errors.append('different test fixtures')
verdict={'verified':not errors,'errors':errors,'source_fixture':list(sha),'results':results,'execution_finished':read(d/'end').strip(),'available':datetime.now(timezone.utc).isoformat()}
(d/'verdict.json').write_text(json.dumps(verdict,indent=2)+'\n');print(json.dumps(verdict));sys.exit(bool(errors))
