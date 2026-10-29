"""Inspect two immutable originals; never execute or download a product binary to a user host."""
import hashlib, http.client, io, json, os, re, struct, tarfile, traceback
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID
ELF = ('e19b03d1c5c44aae539a07dc526d4531c10139e278290660cb49566b77db4920',7368)
ACT = ('68263b241b8d01000d0d245665d98d60c3c9a25d316f8360328238f00bfca4a8',351)
PARENT='91556820-e32c-4ddb-90b3-96f2749c02cd'; SOURCE='5e86e9792316c6b512763bcbdb430beec01e8a37'; TARGET='3631ddbc-bf53-4f00-8f6b-e428da1f3254'
sha=lambda b:hashlib.sha256(b).hexdigest()
class Problem(Exception): pass
def call(key,method,args,n):
 assert method in ('GetFile','GetLog','GetInvocation'); c=http.client.HTTPSConnection('app.buildbuddy.io',timeout=60)
 try:
  c.request('POST','/api/v1/'+method,json.dumps(args),{'Content-Type':'application/json','x-buildbuddy-api-key':key});r=c.getresponse()
  if r.status!=200:raise Problem(str(r.status))
  b=r.read(n+1);assert len(b)<=n;return b
 finally:c.close()
def cas(key,pair):
 h,n=pair; assert re.fullmatch('[0-9a-f]{64}',h) and 0<n<128<<20;b=call(key,'GetFile',{'uri':f'bytestream://remote.buildbuddy.io/blobs/{h}/{n}'},n);assert sha(b)==h and len(b)==n;return b
def fields(b):
 i=0
 def u():
  nonlocal i
  v=0
  for shift in range(0,71,7):
   c=b[i];i+=1;v+=(c&127)<<shift
   if c<128:return v
  raise ValueError('varint')
 while i<len(b):
  t=u();typ=t&7
  if typ==0:v=u()
  elif typ==2:k=u();v=b[i:i+k];i+=k
  else:raise ValueError('wire')
  yield t>>3,v
def one(b,k):
 a=[v for n,v in fields(b) if n==k];assert len(a)==1;return a[0]
def ident(b):return one(b,1).decode(),one(b,2)
def module(k,out):
 b=cas(k,ELF);out['executable']=ELF;assert b[:6]==b'\x7fELF\x02\x01'; eh=struct.unpack_from('<HHIQQQIHHHHHH',b,16);ph=[struct.unpack_from('<IIQQQQQQ',b,eh[4]+i*eh[8]) for i in range(eh[9])];out['machine']=eh[1];out['elf_type']=eh[0];out['interp']=[b[q[2]:q[2]+q[5]].rstrip(b'\0').decode() for q in ph if q[0]==3];assert all(re.fullmatch('/[A-Za-z0-9./_+-]{1,160}',v) for v in out['interp']);needed=[]
 for q in ph:
  if q[0]!=2:continue
  dyn=[struct.unpack_from('<QQ',b,q[2]+i) for i in range(0,q[5],16)];[addr]=[v for t,v in dyn if t==5];[seg]=[x for x in ph if x[0]==1 and x[3]<=addr<x[3]+x[5]];base=seg[2]+addr-seg[3]
  for t,v in dyn:
   if t==1:
    value=b[base+v:b.index(b'\0',base+v)].decode();assert re.fullmatch('[A-Za-z0-9_.+-]{1,80}',value);needed.append(value)
 out['needed']=needed
 act=cas(k,ACT); cmd=cas(k,ident(one(act,1))); args=[v.decode() for n,v in fields(cmd) if n==1]; path='bazel-out/k8-opt-exec-ST-cb2f41ffb59f/bin/external/llvm+/tools/internal/module-map-generator/module-map-generator';assert args[0]==path;out['arg0']=path
 root=ident(one(act,2));out['root']=root
 for seg in path.split('/')[:-1]:
  d=cas(k,root);[ent]=[v for n,v in fields(d) if n==2 and one(v,1).decode()==seg];root=ident(one(ent,2))
 d=cas(k,root);[file]=[v for n,v in fields(d) if n==1 and one(v,1)==b'module-map-generator'];assert ident(one(file,2))==ELF;out['executable_bit']=bool(one(file,4));assert out['executable_bit']

def reasons(text):
 # Only error categories and numeric/source locations; never persist an original log.
 words=['Traceback (most recent call last)','AssertionError','ModuleNotFoundError','TypeError','ValueError','KeyError','AttributeError','FileNotFoundError','CalledProcessError','source overlay digest mismatch','source identity','module resolution','no such package','no such target','not a recognized option','Error in fail','Error in split','Error in transition','Unable to find package','unsupported','no matching toolchains','not executable','linker command failed','Build did NOT complete successfully','failed to fetch','Connection reset','timed out','authentication','Invalid argument','conflicting actions','No module named','was not found','unknown option','nonzero exit code']
 paths=sorted(set(re.findall(r'(?:image_(?:qualification|bootstrap|execution|workload)[.]py|sidecar[.]bzl|MODULE[.]bazel|BUILD(?:[.]bazel)?)[:\", ]+(?:line )?(\d+)',text)))[:24];return {'categories':[w for w in words if w in text],'lines':[int(i) for i in paths], 'modules':sorted(set(re.findall(r"No module named '([a-zA-Z0-9_]{1,64})'",text)))[:8],'missing_attrs':sorted(set(re.findall(r"has no attribute '([a-zA-Z0-9_]{1,64})'",text)))[:8]}
def side(k,out):
 [inv]=json.loads(call(k,'GetInvocation',{'selector':{'invocation_id':PARENT},'include_artifacts':True},8<<20))['invocation']; assert inv['id']['invocationId']==PARENT;out.update(commit=inv.get('commitSha'),status=inv.get('invocationStatus'),duration=inv.get('durationUsec'))
 page='0000';chunks=[];seen=set()
 for _ in range(28):
  assert page not in seen;seen.add(page);log=json.loads(call(k,'GetLog',{'selector':{'invocation_id':PARENT},'page_token':page},3<<20));chunks.append(log.get('log',{}).get('contents',''));page=log.get('nextPageToken','')
  if not page:break
 out['log']=reasons('\n'.join(chunks).split('exec python3 test/rbe/image_bootstrap.py')[-1]);a=[v for v in inv.get('artifacts',[]) if v.get('name')=='qualification.tar.gz'];out['bundle_count']=len(a)
 if not a:return
 [a]=a;u=urlsplit(a['uri']);assert u.scheme=='bytestream' and u.netloc=='remote.buildbuddy.io';m=re.fullmatch(r'/(?:[a-zA-Z0-9_/-]+/)?blobs/([0-9a-f]{64})/(\d+)',u.path);assert m;h=m[1];n=int(m[2]);assert 0<n<=64<<20;b=call(k,'GetFile',{'uri':a['uri']},n);assert len(b)==n and sha(b)==h;out['bundle']=[h,n]
 with tarfile.open(fileobj=io.BytesIO(b)) as t:
  members=t.getmembers();assert len(members)<4096
  def get(name):
   x=[v for v in members if v.isfile() and (v.name==name or v.name.endswith('/'+name))];assert len(x)<=1
   if x:assert x[0].size<36<<20;return t.extractfile(x[0]).read()
  for name in ('verdict.json','image-bootstrap-error.txt','producer-error.txt','workload-error.txt','workload-exception.txt','producer.json'):
   top=t.getnames();hits=[member for member in top if member in (name,'./'+name)]
   if not hits:continue
   m=t.getmember(hits[0]);assert m.isfile() and m.size<4<<20;data=t.extractfile(m).read();out.setdefault('producer_reports',{})[name]=reasons(data.decode(errors='replace'))
   if name=='verdict.json':
    final=json.loads(data);out['producer_status']=final.get('status') if final.get('status') in ('PASS','FAIL','INCOMPLETE') else None
  out['diagnostic_candidate_names']=[m.name for m in members if m.isfile() and re.fullmatch(r'[a-zA-Z0-9_./+-]{1,100}',m.name) and ('error' in m.name or 'diagnostic' in m.name or 'exception' in m.name)][:40]
  v=get('sidecar-review.json');rows=json.loads(v or b'{}');out['phases']={}
  for comp in ('gnu','clang'):
   for arm in ('x86_64','aarch64','tests'):
    phase=comp+'-'+arm;raw=rows.get(phase,{});row=out['phases'][phase]={'typed_summary':phase in rows}; row.update({key:raw[key] for key in ('allocator_query','verified') if type(raw.get(key)) is bool});row['native']=raw.get('native') if raw.get('native') in ('PASS','FAIL','INCOMPLETE') else None;row['error']=reasons(raw.get('diagnostic',''));ev=get('invocation-'+phase+'/events.jsonl')
    for l in (ev or b'').splitlines():
     j=json.loads(l)
     if j.get('started',{}).get('uuid'):row['child']=str(UUID(j['started']['uuid']))
     if 'finished' in j:row['exit']=j['finished']['exitCode'].get('code',0)
     if j.get('aborted',{}).get('description'):row.setdefault('analysis',[]).append(reasons(j['aborted']['description']))
  out['diagnostic_files']=[v.name for v in members if v.isfile() and re.fullmatch(r'(?:invocation-[a-z0-9_-]+/)?(?:traceback|exception|error)(?:[.]txt|[.]json)?',v.name)][:20]
  for name in out['diagnostic_files']:out.setdefault('early_diagnostics',{})[name]=reasons(get(name).decode(errors='replace'))

def main():
 k=os.environ.pop('BUILDBUDDY_API_KEY');out={};rc=0
 for name,fn in [('sidecar',side),('module',module)]:
  row=out[name]={}
  try:fn(k,row);row['read']='complete'
  except Exception as e:row['read']='incomplete';row['error']=type(e).__name__;row['http']=str(e) if isinstance(e,Problem) else None;row['line']=[x.lineno for x in traceback.extract_tb(e.__traceback__) if x.filename.endswith('originals.py')][-4:];rc=1
 Path('verdict.json').write_text(json.dumps(out,indent=2)+'\n');return rc
if __name__=='__main__':raise SystemExit(main())
