import json,os,re
from pathlib import Path
d=Path(os.environ['RUNNER_TEMP']);out=d/'receipt';out.mkdir(exist_ok=True)
result={'reviewed':os.environ['REVIEW'],'strictComparison':os.environ['PRODUCT'],'original':'675d49b1c','qualification':{}}
for phase in ('candidate-launcher','reviewed-owner'):
 bep=d/(phase+'.bep');log=d/(phase+'.log');r={}
 if bep.is_file():
  for line in bep.open():
   e=json.loads(line)
   if 'started'in e:r['invocation']=e['started'].get('uuid')
   if 'finished'in e:r['exitCode']=e['finished'].get('exitCode')
   if 'targetCompleted'in e.get('id',{}):
    t=e['id']['targetCompleted']['label']
    if t in ('//runsc:runsc-plugin-stack','//runsc/sandbox:sandbox_test','//runsc/sandbox:sandbox_nogo'):
     r.setdefault('targetResults',{})[t]=e.get('completed',{}).get('success',False)
   if 'testSummary'in e:
    r.setdefault('tests',{})[e['id']['testSummary']['label']]=e['testSummary'].get('overallStatus')
 if log.is_file() and not r.get('exitCode',{}).get('name')=='SUCCESS':
  r['diagnostics']=[s[:400] for s in log.read_text(errors='replace').splitlines() if re.search(r'\b(ERROR:|error:|FAILED:|No space left)',s) and not re.search('credential|token|header|secret|api.key',s,re.I)][-10:]
 result['qualification'][phase]=r
(out/'arena-owners.json').write_text(json.dumps(result,indent=2)+'\n')
