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
def lint(k,out):
 h='e9bc748dafbf9d7eb776d6fca3f83b44beb3b258b5f6461698bb919ff43b2a87';n=163075;b=cas(k,(h,n));out['bundle']=[h,n]
 with tarfile.open(fileobj=io.BytesIO(b)) as t:
  members=t.getmembers();assert len(members)<4096
  for m in members:
   if not m.isfile():continue
   stage,_,name=m.name.rpartition('/');stage=stage.split('/')[-1];key=stage+'/'+name
   if not re.fullmatch('invocation-[0-9]{2}[-a-z0-9_]+',stage) or name not in ('stdout.log','stderr.log','native-exit','arguments.txt'):continue
   assert m.size<8<<20
   raw=t.extractfile(m).read();out.setdefault('members',{})[key]={'sha256':sha(raw),'bytes':len(raw)}
   if name=='native-exit':out.setdefault('exit',{})[stage]=int(raw);continue
   if name=='arguments.txt':continue
   text=raw.decode(errors='replace'); assert k not in text;out[key]={'diagnostics':[]};clean=re.sub(r'\x1b\[[;\d]*[A-Za-z]','',text)
   for line in clean.splitlines():
    if not re.search(r'error|warning|failed|reformat|timed out|permission denied|not found|invalid|mismatch|must be updated|expected|no such|returned|Traceback|^--- |^\+\+\+ |^buildifier|requires|unused|uninitialized|reassigned|unused-variable|is already defined',line,re.I):continue
    # Bound each diagnostic to human-readable prose/known paths. No command lines, digests, raw logs, or URLs leave the job.
    if len(line)>1200 or re.search(r'(?i)credential|password|secret|api.key|authorization|--remote.header|BEGIN [A-Z ]+ KEY',line):continue
    line=re.sub(r'https?://\S+','[url]',line);line=re.sub(r'[A-Za-z0-9_+/=-]{36,}','[id]',line);line=re.sub(r'/[^ :]+/(?:external|execroot)/','[execroot]/',line)
    line=''.join(v for v in line if v=='\t' or ord(v)>=32 and ord(v)<127)[:300];assert k not in line
    if line not in out[key]['diagnostics']:out[key]['diagnostics'].append(line)
    if len(out[key]['diagnostics'])>=18:break
  out['read']='complete'

def main():
 k=os.environ.pop('BUILDBUDDY_API_KEY');out={};rc=0
 try:lint(k,out)
 except Exception as e:out['error']=type(e).__name__;out['line']=[x.lineno for x in traceback.extract_tb(e.__traceback__) if x.filename.endswith('lint.py')][-4:];rc=1
 Path('verdict.json').write_text(json.dumps(out,indent=2)+'\n');return rc
if __name__=='__main__':raise SystemExit(main())
