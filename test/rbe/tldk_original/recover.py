"""Materialize only the original Bazel outputs by their address; never rebuild."""
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

RUN='ca7396df-9b3d-415f-9c0c-8899dbaaf9ad'
TEST='2bce7a06-ddb8-4d2b-9fca-4d729d92c63c'
root=Path(os.environ['RUNNER_TEMP'])
dest=root/'reused'
out=root/'receipt'
out.mkdir(exist_ok=True)
seen={RUN:[],TEST:[]}
errors=[]
labels={RUN:['//:release','//runsc:runsc-plugin-stack','//runsc/cmd/sentry:gvisor_sentry_plugin_stack'],TEST:['//test/e2e:integration_test']}
expected={'runsc':'f9d549280a7ff966e3d5959b2808392dbfda62552af018317a56317a7acc3247','gvisor-bin/gvisor_sentry_plugin_stack':'7a272c8e0d4556b0879d8e65a6af9fd1865bfc5ef947dcbd7b15ebe8b78ac32a'}
names={**{k.split('/')[-1]:(RUN,k) for k in expected},**{k:(RUN,'gvisor-bin/'+k) for k in ['checkpointgofer','runsc-metric-server','runsc-fd-parking','gvisor-sentry-prewarmer','gvisor-proc-umounter','gvisor_sentry']},'integration_test':(TEST,'original-test')}
names['runsc-plugin-stack']=names.pop('runsc')
api='https://app.buildbuddy.io/'
headers={'Content-Type':'application/json','x-buildbuddy-api-key':os.environ['BUILDBUDDY_API_KEY']}
def req(path,data):
 return urllib.request.urlopen(urllib.request.Request(api+path,data=json.dumps(data).encode(),headers=headers),timeout=120)
def call(path,d):
 with req(path,d) as r:return json.load(r)
def walk(v):
 if isinstance(v,list):
  for x in v:yield from walk(x)
 elif isinstance(v,dict):
  yield v
  for x in v.values():
   if isinstance(x,(list,dict)):yield from walk(x)
def record(i,data):
 for x in walk(data):
  uri=x.get('uri','')
  path=x.get('name') or x.get('path') or (uri if isinstance(uri,str) else '')
  if not isinstance(path,str):continue
  name=path.split('/')[-1]
  if name not in names or names[name][0]!=i:continue
  h=x.get('hash') or x.get('digest') or ''
  size=x.get('sizeBytes') or x.get('length') or 0
  if isinstance(h,dict):size,h=h.get('sizeBytes'),h.get('hash')
  if isinstance(uri,str) and uri.startswith('bytestream://'):
   m=re.search(r'/blobs/([0-9a-f]{64})/(\d+)$',uri)
   if m:h,size=m.groups()
  if not isinstance(h,str) or not re.fullmatch('[0-9a-f]{64}',h) or not size:continue
  loc=names[name][1]
  # Preserve the original installed plugin binary even if a similarly named
  # output appears in a configuration or test data group.
  if loc in expected and h!=expected[loc]:continue
  item=(loc,h,int(size),name)
  if item not in seen[i]:seen[i].append(item)
def missing(i):
 return sorted({v[1] for v in names.values() if v[0]==i}-{x[0] for x in seen[i]})
def api_read(path,body,i):
 try:
  r=call(path,body);record(i,r)
 except (urllib.error.URLError,ValueError,TimeoutError) as e:
  errors.append([path,body.get('targetLabel',''),getattr(e,'code',None),type(e).__name__])
 else:return r

def manifest(status,outputs=None):
 (out/'original-actions.json').write_text(json.dumps({'status':status,'metadataErrors':errors,'invocations':seen,'missing':{i:missing(i) for i in labels},'outputs':outputs or {}},indent=2)+'\n')

for i,ls in labels.items():
 for l in ls:
  # A target build's named outputs can be retrieved separately from test.log.
  api_read('rpc/BuildBuddyService/GetTarget',{'invocationId':i,'targetLabel':l},i)
  api_read('api/v1/GetAction',{'selector':{'invocation_id':i,'target_label':l}},i)
 if missing(i):
  api_read('rpc/BuildBuddyService/GetInvocation',{'lookup':{'invocationId':i}},i)
 if missing(i):
  extra={'checkpointgofer':'//runsc/checkpointgofer:checkpointgofer_binary','runsc-metric-server':'//runsc/cmd/metricserver:runsc-metric-server','runsc-fd-parking':'//runsc/fdparking:fdparking_binary','gvisor-sentry-prewarmer':'//runsc/prewarmer:prewarmer_binary','gvisor-proc-umounter':'//runsc/procumounter:procumounter_binary','gvisor_sentry':'//runsc/cmd/sentry:gvisor_sentry'}
  # When a release alias was materialized locally, recover the producing
  # remote link's identical bytes by its real output path; record that path.
  alias={'checkpointgofer_binary':'checkpointgofer','fdparking_binary':'runsc-fd-parking','prewarmer_binary':'gvisor-sentry-prewarmer','procumounter_binary':'gvisor-proc-umounter'}
  if i==RUN:
   for src,tgt in alias.items():names[src]=names[tgt]
  for l in dict.fromkeys(ls+([extra[x] for x in extra if 'gvisor-bin/'+x in missing(i)] if i==RUN else [])):
   api_read('rpc/BuildBuddyService/GetExecution',{'executionLookup':{'invocationId':i,'targetLabel':l},'inlineExecuteResponse':True},i)
 manifest('INVENTORY')
required=set(expected)|{'original-test'}
absent=set().union(*(missing(i) for i in labels))
assert not required.intersection(absent),{'missingRequired':sorted(required.intersection(absent))}
outputs={}
for i,rows in seen.items():
 for loc in sorted({x[0] for x in rows}):
  choices=set((h,size) for l,h,size,name in rows if l==loc)
  assert len(choices)==1,(loc,choices)
  h,size=choices.pop()
  # Native Linux binary only, not scripts from any path in the input record.
  p=dest/loc;p.parent.mkdir(parents=True,exist_ok=True)
  uri='bytestream://remote.buildbuddy.io/blobs/'+h+'/'+str(size)
  try: response=req('api/v1/GetFile',{'uri':uri})
  except urllib.error.HTTPError as err:
   if loc in required:raise
   errors.append(['unavailable optional sidecar',loc,err.code]);continue
  with response as r, p.open('wb') as f:
   d=hashlib.sha256();n=0
   while True:
    part=r.read(1024*1024)
    if not part:break
    f.write(part);d.update(part);n+=len(part)
  assert n==size and d.hexdigest()==h,(loc,n,size,d.hexdigest(),h)
  with p.open('rb') as verify:head=verify.read(20)
  assert head[:6]==b'\x7fELF\x02\x01' and int.from_bytes(head[18:20],'little')==62,(loc,head)
  p.chmod(0o755);outputs[loc]={'sha256':h,'bytes':size};manifest('DOWNLOADING',outputs)
manifest('PASS',outputs)
print('verified original outputs:',sorted(outputs))
