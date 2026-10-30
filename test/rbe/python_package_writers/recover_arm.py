"""Read the original three actions. No Bazel invocation or writer runs here."""
from pathlib import Path
import http.client
import json
import os
import posixpath
import struct
from datetime import datetime,timezone
from original_cas import fields, ident, node, get, key, sha

INV="52fed88e-8a4b-4d15-ae72-aac715bb0571"
HEAD="3eba5b520169be576c2e09d30b559b75950fac21"
ARM=["6fd428f3883d8b660ab82562603e5d4adbf45349dd2c2ea77e87e1a4de567557",44747056]
IMAGE="docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
EXPECTED={"PackageTar":("payload","payload.tar","3a57a5fe4121f867ccd0c26c89c511dd0f5851712ae6723babed02f950193898"),"MakeDeb":("package",".deb","486d93a4ea624b12536af762811caeac49ab801c05169c7dc744d737673ab015"),"RelocatedPythonWriter":("relocated","relocated.json",None)}
def call(path,body):
 c=http.client.HTTPSConnection("app.buildbuddy.io",timeout=30)
 try:
  c.request("POST",path,json.dumps(body),{"Content-Type":"application/json","x-buildbuddy-api-key":key})
  r=c.getresponse()
  assert r.status==200,(path,r.status)
  raw=r.read(12*1024*1024); assert len(raw)<12*1024*1024
  return json.loads(raw,object_hook=lambda d: {k.split('_')[0]+''.join(part.title() for part in k.split('_')[1:]): value for k,value in d.items()})
 finally:c.close()
def dg(x):return [x['hash'],int(x.get('sizeBytes',0))]
def pathnode(root,path):
 for n in range(10):
  x=node(root,path)
  assert x,(root,path)
  if x['kind']!='symlink':return x
  tail=x['remaining'];pre=path.split('/')[:len(path.split('/'))-len(tail)-1]
  path=posixpath.normpath('/'.join([*pre,x['target'],*tail]))
  assert not path.startswith(('/','../')),path
 raise AssertionError('symlink cycle')
def main(out):
 old=json.loads(Path('previous/native-launcher.json').read_text())
 assert old['source']==HEAD and old['invocation']==INV and old['native_exit']==0
 graph=json.loads(Path('previous/native-graph.json').read_text())
 orig=call('/api/v1/GetInvocation',{"selector":{"invocation_id":INV},"include_artifacts":True})
 [iv]=orig["invocation"]; assert iv["id"]["invocationId"]==INV
 if iv.get('commitSha'): assert iv['commitSha']==HEAD,iv['commitSha']
 out['invocation_status']=iv.get('invocationStatus',iv.get('status'))
 for mnemonic,(name,suffix,expected) in EXPECTED.items():
  executions=[]
  label='//test/rbe/python_package_writers:'+name
  for target in (label,'@@'+label):
   j=call('/rpc/BuildBuddyService/GetExecution',dict(executionLookup=dict(invocationId=INV,targetLabel=target),inlineExecuteResponse=True))
   executions.extend(x for x in j.get('execution',[]) if x.get('actionMnemonic')==mnemonic)
   if executions:break
  [x]=executions; result=out['workers'][mnemonic]={}
  assert x['actionMnemonic']==mnemonic and int(x.get('exitCode',0))==0 and int(x.get('status',{}).get('code',0))==0
  result['action']=dg(x['actionDigest']);ac=get(result['action'])
  from original_cas import one
  root=ident(one(ac,2));cmd=get(ident(one(ac,1)));argv=[v.decode() for k,v in fields(cmd) if k==1]
  properties={}
  for platform in (one(ac,10),one(cmd,5)):
   if platform:
    properties.update({one(v,1).decode():one(v,2).decode() for k,v in fields(platform) if k==1})
  result['platform']=properties
  assert properties.get('Arch')=='arm64' and properties.get('container-image')==IMAGE,properties
  resp=x.get('executeResponse',{});assert not resp.get('cachedResult',False) and int(resp.get('status',{}).get('code',0))==0
  action_result=resp.get('result',{}); assert int(action_result.get('exitCode',0))==0
  if action_result.get('outputFiles'):
   [of]=[o for o in action_result['outputFiles'] if o['path'].endswith(suffix)];od=dg(of['digest'])
  else:
   assert x.get('executedActionMetadata') and label==x.get('targetLabel','').removeprefix('@@'), (mnemonic, list(x))
   urls={a['uri'] for a in iv.get('artifacts',[]) if a.get('name','').endswith(suffix) and a.get('uri','').startswith('bytestream://')}
   [uri]=urls;h,n=uri.rsplit('/',2)[-2:];od=[h,int(n)]
  b=get(od);result['output']=od
  if expected:assert od[0]==expected,od
  if mnemonic=='RelocatedPythonWriter':
   result['manifest']=z=json.loads(b); assert z['interpreter']==ARM[0] and z['relocated_exit']==0 and z['missing_prefix_exit']!=0 and z['interpreter_importtime'] and z['independent_roots_adapter_missing_root']
   continue
  writer=argv[0].rsplit('/',1)[-1];g=graph[writer];assert argv[0]==g['executable']
  exe=pathnode(root,argv[0]);launch=get(exe['digest']);assert launch[:5]==b'\x7fELF\x02' and int.from_bytes(launch[18:20],'little')==183
  off=struct.unpack_from('<Q',launch,32)[0];sz,num=struct.unpack_from('<HH',launch,54);assert all(struct.unpack_from('<I',launch,off+sz*i)[0]!=3 for i in range(num))
  result['launcher']=exe['digest'];result['bootstrap']=pathnode(root,argv[0]+'.runfiles/'+g['adapter'])['digest']
  runtime=argv[0]+'.runfiles/'+g['interpreter'].split('/')[0]
  result['runtime_dir']=pathnode(root,runtime);result['interpreter']=pathnode(root,argv[0]+'.runfiles/'+g['interpreter'])['digest']
  assert result['interpreter']==ARM,(result['interpreter'],ARM)
  if mnemonic=='MakeDeb':
   prev=old['workers']['MakeDeb'];assert dg(prev['action'])==result['action'] and prev['expanded_bootstrap_bytes']==result['bootstrap'][1] and prev['python_bootstrap_sha256']==result['bootstrap'][0] and prev['shared_runtime']['interpreter']==ARM
   result['common_footprint']=prev['shared_runtime']
 tar,deb,rel=(out['workers'][k] for k in EXPECTED)
 assert tar['runtime_dir']==deb['runtime_dir'] and tar['runtime_dir']['kind']=='dir'
 assert rel['manifest']['writer']==tar['launcher'][0]
 out['status']='PASS'
out={'source':HEAD,'invocation':INV,'workers':{},'status':'INCOMPLETE'}
try: main(out)
finally:
 out['verdict_at']=datetime.now(timezone.utc).isoformat()
 Path('recovered.json').write_text(json.dumps(out,indent=2)+'\n')
 print('Original source:',HEAD,'verdict:',out['status'],'workers:',list(out['workers']))
