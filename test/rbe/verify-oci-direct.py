import json,sys
from datetime import datetime,timezone
from pathlib import Path
d=Path(sys.argv[1]);err=[];arms={};fixtures=[]
def read(p):return p.read_text().strip() if p.is_file() else ''
for name,src in [('stock','d624fba03a2b3f1f9a314e54198c95460005cc15'),('fixed','c89f8f55abfe0d08c2bfc836192bd27d0aee7161')]:
    a=d/name
    fixtures.append(read(a/'test.sha256'))
    if read(a/'source')!=src or read(a/'build.exit')!='0':err.append('unbuilt source: '+name)
    rows={}
    for label,leaf in [('unlimited',None),('larger',768<<20),('smaller',384<<20)]:
        try: row=json.loads(read(a/(label+'.json')))
        except ValueError:row={}
        v=row.get('MemTotal')
        levels=[str(512<<20),'max','max' if leaf is None else str(leaf)]
        correct=v==min(512<<20,leaf or 512<<20)
        old_expected=v is not None and (v>(512<<20) if leaf is None else v==leaf)
        if row.get('status')!=0 or row.get('limits')!=levels or not (correct if name=='fixed' else old_expected):err.append(name+' '+label+' failed or not run')
        rows[label]=row
    arms[name]={'source':src,'expected_cases':3,'executed_cases':sum(v.get('MemTotal') is not None for v in rows.values()),'results':rows}
if not fixtures[0] or fixtures[0]!=fixtures[1]:err.append('mismatched source fixtures')
verdict={'verified':not err,'errors':err,'arms':arms,'end':read(d/'end'),'available':datetime.now(timezone.utc).isoformat()}
(d/'verdict.json').write_text(json.dumps(verdict,indent=2)+'\n');print(json.dumps(verdict));sys.exit(bool(err))
