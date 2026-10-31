import json,os,re,sys
from pathlib import Path
t=Path(os.environ['RUNNER_TEMP']);d=t/'traffic';r={'product':os.environ['PRODUCT'],'stage':(d/'stage').read_text().strip() if (d/'stage').exists() else 'host setup','tests_executed':[],'tests_passed':[],'traffic':'not established'}
def read(p):return p.read_text(errors='replace') if p.exists() else ''
for name in ('binaries','guest','paths'):
 lines=read(t/(name+'.log')).splitlines()
 if lines:
  allowed=[l[:750] for l in lines if any(x in l for x in ('ERROR:','error:','FATAL','not found','failed:', 'error while', 'Killed','FAILED','DOCKER')) and not any(x in l.lower() for x in ('remote_header','bes_header','api-key','buildbuddy_api_key'))];(d/(name+'.diagnostics')).write_text('\n'.join(allowed[-35:])+'\n')
status='FAIL'
try:
 device=json.loads((d/'device.json').read_text())
 if device['status']=='HOST_DEVICE_BLOCKED':
  status='BLOCKED_NO_DEVICE'
 else:
  assert device['status']=='PASS'
  assert int((d/'test-exit').read_text())==0
  lines=read(d/'test.log').replace('\x16','')
  name='TestConnectToSelf'
  for field,pattern in [('tests_executed',r'^\s*=== RUN\s+(\S+)'),('tests_passed',r'^\s*--- PASS:\s+(\S+)')]:
   r[field]=re.findall(pattern,lines,re.M)
   assert r[field]==[name],(field,r[field])
  assert not re.search(r'^\s*--- (SKIP|FAIL):',lines,re.M)
  rows=[json.loads(l) for l in (t/'e2e.bep').read_text().splitlines()]
  results=[e['testResult'] for e in rows if 'testResult' in e];assert len(results)==1
  a=results[0];r['execution']=a['executionInfo'];assert r['execution']['strategy'] in ('local','standalone') and not a.get('cachedLocally') and not a['executionInfo'].get('cachedRemotely') and a['status']=='PASSED'
  r['traffic']='client received server newline; server produced client newline on port 8080';status='PASS'
except Exception as e:r['error']=repr(e)
for kind in ('plugin','e2e'):
 p=t/(kind+'.bep')
 if p.exists():
  with p.open() as f:
   for l in f:
    event=json.loads(l)
    if 'started' in event:r[kind+'_invocation']=event['started']['uuid'];break
r['status']=status;(d/'verdict.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r));sys.exit(1 if status=='FAIL' else 0)
